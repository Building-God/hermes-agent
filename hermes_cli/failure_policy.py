"""
Unified failure handling policy for the Hermes dispatcher and workers.

Informed by Temporal, Sidekiq, AWS SQS, and Hystrix patterns.
Every failure gets classified → transient/permanent/needs-harry → retry policy flows.

The dispatcher consults this module before every spawn:
  - Check if (profile, provider) is in cooldown (circuit breaker)
  - Calculate next backoff if transient
  - Escalate if permanent or needs_harry
  
Workers use it to signal failure outcomes clearly.
"""

import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple


class FailureClass(Enum):
    """Failure classification for retry policy decisions."""
    
    TRANSIENT = "transient"
    """Network, timeout, rate-limit, service temporarily down.
    Retry with exponential backoff. Example: 503 ServiceUnavailable."""
    
    PERMANENT = "permanent"
    """Bad credentials, invalid input, malformed spec, permission denied.
    Never blind-retry. Escalate to orchestrator. Example: 401 Unauthorized."""
    
    CAPACITY = "capacity"
    """Rate limit, quota exhausted. Special transient: needs circuit breaker
    per (profile, provider) to avoid self-DoS. Example: 429 TooManyRequests."""
    
    NEEDS_HARRY = "needs_harry"
    """Physical action, spend approval, human decision. One tappable card only.
    Example: "Approve $500 spend to continue"."""
    
    INFRASTRUCTURE = "infrastructure"
    """Host/dispatcher refusal (not card's fault). Retry forever, spaced.
    Example: spawn denied due to no restart-safe scope."""


@dataclass
class FailurePolicy:
    """Per-failure retry and escalation policy."""
    
    failure_class: FailureClass
    error_text: str
    """Full error message for logging and dead-letter queue."""
    
    # Backoff parameters (for TRANSIENT/CAPACITY classes)
    backoff_base_seconds: int = 60
    backoff_coefficient: float = 2.0
    backoff_max_seconds: int = 30 * 60  # 30 minutes
    backoff_jitter_percent: int = 20    # ±20%
    
    # Retry budget
    max_retries: int = 3
    """Total retry budget across all runs for this task."""
    
    # Circuit breaker per (profile, provider)
    circuit_breaker_threshold: int = 3
    """Consecutive failures before circuit opens."""
    circuit_breaker_cooldown_seconds: int = 15 * 60  # 15 minutes
    """Cooldown before attempting half-open."""
    
    def next_backoff_seconds(self, attempt: int) -> int:
        """
        Calculate next backoff delay for this attempt (1-indexed).
        
        Formula: min(base * (coefficient ^ attempt), max) + jitter(±percent)
        
        Example sequence (base=60, coeff=2, max=1800):
        - attempt 1: 60s ± 12s
        - attempt 2: 120s ± 24s
        - attempt 3: 240s ± 48s
        - attempt 4+: 1800s ± 360s (capped)
        """
        if attempt < 1:
            attempt = 1
        
        raw = int(self.backoff_base_seconds * (self.backoff_coefficient ** (attempt - 1)))
        capped = min(raw, self.backoff_max_seconds)
        
        # Add jitter: ±percent
        jitter_amount = int(capped * self.backoff_jitter_percent / 100)
        jitter = random.randint(-jitter_amount, jitter_amount)
        
        return max(0, capped + jitter)


# Pattern-based failure classification
_TRANSIENT_PATTERNS = re.compile(
    r"\b("
    r"timeout|timed out|time.?out|deadline.*exceeded|"
    r"connection.*reset|connection.*closed|connection.*refused|"
    r"network.*unreachable|no.*route|"
    r"503|service.*unavailable|"
    r"504|gateway.*timeout|"
    r"temporarily.*unavailable|"
    r"EAGAIN|ECONNRESET|EHOSTDOWN|ECONNREFUSED|"
    r"temporary|transient|retry.*later"
    r")\b",
    re.IGNORECASE
)

_PERMANENT_PATTERNS = re.compile(
    r"\b("
    r"401|unauthorized|authenticat(?:e|es|ed|ing|ion)|"
    r"authoriz(?:e|es|ed|ing|ation)|authoris(?:e|es|ed|ing|ation)|authz|"
    r"403|forbidden|permission.*denied|access.*denied|"
    r"404|not.*found|"
    r"400|bad.*request|invalid.*input|malformed|invalid.*spec|"
    r"501|not.*implemented|"
    r"validation.*error|"
    r"credentials|api.*key|secret|token|"
    r"missing.*required.*field"
    r")\b",
    re.IGNORECASE
)

_CAPACITY_PATTERNS = re.compile(
    r"\b("
    r"429|rate.*limit|rate.?limited|quota|"
    r"too.*many.*requests|requests.*per|"
    r"capacity.*exceeded|resource.*exhausted|"
    r"backoff|retry.?after|"
    r"request.*rate"
    r")\b",
    re.IGNORECASE
)


def classify_failure(error_text: str, outcome: Optional[str] = None) -> FailurePolicy:
    """
    Classify a failure and return its retry policy.
    
    Args:
        error_text: The error message/traceback
        outcome: Optional outcome from kanban (e.g., "rate_limited", "spawn_failed")
    
    Returns:
        FailurePolicy with class and retry parameters
    """
    error_lower = error_text.lower() if error_text else ""
    
    # Outcome precedence (dispatcher signals these explicitly)
    if outcome == "rate_limited":
        return FailurePolicy(
            failure_class=FailureClass.CAPACITY,
            error_text=error_text or "Rate limited by provider",
            max_retries=3,
        )
    
    if outcome == "spawn_failed" and "infrastructure" in error_lower:
        return FailurePolicy(
            failure_class=FailureClass.INFRASTRUCTURE,
            error_text=error_text or "Host spawn refusal",
            max_retries=999,  # Retry forever, spaced
        )
    
    # Error text pattern matching (highest precedence = most specific)
    if _CAPACITY_PATTERNS.search(error_lower):
        return FailurePolicy(
            failure_class=FailureClass.CAPACITY,
            error_text=error_text,
            max_retries=3,
        )
    
    if _PERMANENT_PATTERNS.search(error_lower):
        return FailurePolicy(
            failure_class=FailureClass.PERMANENT,
            error_text=error_text,
            max_retries=1,  # One attempt max, then escalate
        )
    
    if _TRANSIENT_PATTERNS.search(error_lower):
        return FailurePolicy(
            failure_class=FailureClass.TRANSIENT,
            error_text=error_text,
            max_retries=3,
        )
    
    # Default: assume transient (most conservative for unknown errors)
    return FailurePolicy(
        failure_class=FailureClass.TRANSIENT,
        error_text=error_text or "Unknown error",
        max_retries=3,
    )


@dataclass
class CircuitBreakerState:
    """Per-(profile, provider) circuit breaker state."""
    
    profile: str
    provider: str
    consecutive_failures: int = 0
    last_failure_time: Optional[float] = None
    state: str = "closed"  # closed, open, half_open
    
    def is_open(self, now: Optional[float] = None) -> bool:
        """Return True if the circuit is currently OPEN (fail-fast)."""
        if now is None:
            now = time.time()
        
        if self.state == "closed":
            return False
        
        if self.state == "open":
            # Check if cooldown has elapsed → try half-open
            if self.last_failure_time and (now - self.last_failure_time) > 15 * 60:
                self.state = "half_open"
                return False
            return True
        
        # half_open: let the request through (not "open")
        return False
    
    def record_failure(self, threshold: int = 3) -> bool:
        """
        Record a failure; return True if circuit just opened.
        
        Typically called when classify_failure returns CAPACITY class
        and the error pattern indicates a provider issue.
        """
        self.consecutive_failures += 1
        self.last_failure_time = time.time()
        
        if self.consecutive_failures >= threshold:
            self.state = "open"
            return True
        
        return False
    
    def record_success(self) -> None:
        """Record a success; reset the circuit to CLOSED."""
        self.consecutive_failures = 0
        self.state = "closed"
        self.last_failure_time = None


class CircuitBreakerRegistry:
    """In-memory registry of per-(profile, provider) circuit breakers.
    
    In production this would be persisted in the kanban DB.
    For now, in-memory for the dispatcher process.
    """
    
    def __init__(self):
        # (profile, provider) -> CircuitBreakerState
        self._breakers: Dict[Tuple[str, str], CircuitBreakerState] = {}
    
    def get_or_create(self, profile: str, provider: str) -> CircuitBreakerState:
        """Get or create a circuit breaker for this (profile, provider)."""
        key = (profile, provider)
        if key not in self._breakers:
            self._breakers[key] = CircuitBreakerState(profile, provider)
        return self._breakers[key]
    
    def is_open(self, profile: str, provider: str, now: Optional[float] = None) -> bool:
        """Check if the (profile, provider) circuit is currently open."""
        breaker = self.get_or_create(profile, provider)
        return breaker.is_open(now)
    
    def record_failure(self, profile: str, provider: str) -> bool:
        """Record failure; return True if circuit just opened."""
        breaker = self.get_or_create(profile, provider)
        return breaker.record_failure()
    
    def record_success(self, profile: str, provider: str) -> None:
        """Record success; reset the circuit."""
        breaker = self.get_or_create(profile, provider)
        breaker.record_success()


# Global registry (one per dispatcher process)
_global_registry = CircuitBreakerRegistry()


def get_registry() -> CircuitBreakerRegistry:
    """Get the global circuit breaker registry."""
    return _global_registry


# ============================================================================
# Worker integration: outcomes that workers should signal
# ============================================================================

def worker_outcome_for_failure(policy: FailurePolicy) -> str:
    """
    Return the kanban outcome string a worker should use when signaling this failure.
    
    Outcomes have specific meanings to the dispatcher:
    - "rate_limited": special per-profile cooldown
    - "nonzero_exit": regular failure (tries again if budget remains)
    - "timed_out": timeout-specific handling
    """
    if policy.failure_class == FailureClass.CAPACITY:
        return "rate_limited"
    
    # All other failures: exit with appropriate code
    # (workers use kanban_block with reason instead of exit codes)
    return "nonzero_exit"


# ============================================================================
# Dispatcher integration: blocked-card validator
# ============================================================================

def validate_needs_input_block(reason: str) -> Tuple[bool, Optional[str]]:
    """
    Validate that a needs_input block genuinely requires Harry, not agent action.
    
    Returns (is_valid, error_message).
    
    Valid needs_input scenarios:
    - "Approve $X spend"
    - "Enter the 2FA code sent to your email"
    - "Confirm you want to delete X"
    - "Choose between A, B, or C"
    
    Invalid (agent-doable):
    - "Create a GitHub account" (browser + OAuth)
    - "Upload a file" (agent can do this)
    - "Copy-paste this value" (agent can navigate, type, send)
    """
    agent_doable_patterns = re.compile(
        r"\b("
        r"create.*account|sign.*up|"
        r"upload|download|"
        r"navigate|click|"
        r"copy|paste|enter.*value|type|"
        r"run.*command|execute|"
        r"fill.*form|submit.*form"
        r")\b",
        re.IGNORECASE
    )
    
    harry_only_patterns = re.compile(
        r"\b("
        r"approve|confirm|decide|choose|"
        r"2fa|two.?factor|code.*sent|"
        r"passphrase|password|secret|"
        r"physical|in-person|"
        r"consent|authorize|permission"
        r")\b",
        re.IGNORECASE
    )
    
    # Fail closed: if agent-doable patterns match, invalid
    if agent_doable_patterns.search(reason):
        return False, f"This is agent-doable, not Harry-only: {reason}"
    
    # Valid if explicitly mentions Harry actions
    if harry_only_patterns.search(reason):
        return True, None
    
    # Unknown: require more specificity
    return False, f"Unclear if genuinely Harry-only: {reason}"


# ============================================================================
# Exponential backoff calculation (standalone utility)
# ============================================================================

def calculate_backoff_sequence(
    base: int = 60,
    coefficient: float = 2.0,
    max_seconds: int = 30 * 60,
    attempts: int = 5,
) -> list:
    """Generate a backoff sequence for documentation/testing."""
    sequence = []
    for attempt in range(1, attempts + 1):
        raw = int(base * (coefficient ** (attempt - 1)))
        capped = min(raw, max_seconds)
        sequence.append(capped)
    return sequence
