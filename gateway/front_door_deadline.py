"""Bounded front-door turn deadline: truthful acknowledgement + durable tracked continuation.

The gateway's per-turn agent loop is bounded by an *inactivity* timeout
(``agent.gateway_timeout`` / ``HERMES_AGENT_TIMEOUT``) that only fires when the
agent goes completely idle. A turn that keeps making slow provider/tool calls
stays "active", so that watchdog never fires and a simple status/why question can
spin for 20+ minutes with no response-ready event (observed 2026-10-01 06:07:28
AEST: one Discord turn ran ``time=1349.0s api_calls=22`` before ``response ready``).

This module adds a *wall-clock* deadline that is independent of activity. When a
chat turn outlives the deadline before producing a final response, the gateway
instead:

1. journals the request durably to a JSONL file (survives gateway restart), keyed
   by an idempotency key so a restart/replay never answers the same request twice;
2. emits a truthful bounded acknowledgement naming the tracking id (never a false
   "still working" ping, never silence);
3. hands back a bounded result so the provider wait / tool loop is recovered and
   the deeper work continues as a tracked record instead of an unbounded turn.

The feature is opt-in: ``agent.gateway_turn_deadline`` (seconds, env
``HERMES_AGENT_TURN_DEADLINE``) defaults to ``0`` (disabled) so existing installs
are unchanged until an operator enables it with a measured value. This is the
safe scoped remedy for the observed failure; it does not change the global model
selection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("gateway.front_door_deadline")

# 0 = disabled. Opt-in so no existing install changes behaviour without an
# operator choosing a value (the task forbids changing defaults on guesswork).
DEFAULT_DEADLINE_SECONDS = 0.0

JOURNAL_FILENAME = "front_door_deadline_journal.jsonl"


def resolve_front_door_deadline(raw: Any, env_value: Optional[str] = None) -> float:
    """Resolve the wall-clock front-door deadline in seconds.

    Precedence: ``env_value`` (already-bridged ``HERMES_AGENT_TURN_DEADLINE``) if
    it parses as a positive float, else ``raw`` (the config.yaml
    ``agent.gateway_turn_deadline`` value), else 0. Non-positive values mean
    "disabled" and are normalised to 0.0 so callers only ever compare against a
    strict ``> 0`` deadline.
    """
    candidates = [env_value, raw]
    for value in candidates:
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0.0


def journal_path(hermes_home: Any) -> Path:
    """Absolute path of the durable deadline journal under ``hermes_home``."""
    return Path(hermes_home) / JOURNAL_FILENAME


def idempotency_key(platform: str, conversation_id: str, request_ts: float) -> str:
    """Stable key for one inbound request: same conversation + same second.

    Used so a gateway restart that re-delivers the same inbound message (or a
    replayed session resume) can detect that the request was already journaled and
    acknowledged, instead of answering it twice or silently dropping it.
    """
    return f"{platform}:{conversation_id}:{int(request_ts)}"


def tracking_id(platform: str, conversation_id: str, request_ts: float) -> str:
    """Short stable tracking id for a request (human-readable, unique per request)."""
    digest = hashlib.sha1(
        f"{platform}:{conversation_id}:{int(request_ts)}".encode("utf-8")
    ).hexdigest()[:8]
    return f"fd-{int(request_ts)}-{digest}"


def make_request_entry(
    *,
    platform: str,
    conversation_id: str,
    session_key: Optional[str],
    user: Optional[str],
    message: str,
    request_ts: float,
    deadline_seconds: float,
    ack_ts: Optional[float] = None,
    card_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the durable journal entry for one request."""
    _ts = request_ts if request_ts else time.time()
    _key = idempotency_key(platform, conversation_id, _ts)
    return {
        "tracking_id": tracking_id(platform, conversation_id, _ts),
        "idempotency_key": _key,
        "platform": platform,
        "conversation_id": str(conversation_id),
        "session_key": session_key,
        "user": user,
        "message": message,
        "request_ts": _ts,
        "request_ts_iso": datetime.fromtimestamp(_ts, tz=timezone.utc).isoformat(),
        "deadline_seconds": deadline_seconds,
        "ack_ts": ack_ts,
        "ack_ts_iso": (
            datetime.fromtimestamp(ack_ts, tz=timezone.utc).isoformat()
            if ack_ts is not None
            else None
        ),
        "status": "acknowledged",
        "source": "front_door_deadline",
        "card_id": card_id,
    }


def journal_request(hermes_home: Any, entry: Dict[str, Any]) -> Path:
    """Append ``entry`` to the durable journal, skipping an already-journaled key.

    Returns the journal path. The append is a single line per request; the
    idempotency check is a defensive second guard on top of the caller's
    once-per-turn flag so a restart/replay can never double-answer.
    """
    path = journal_path(hermes_home)
    key = entry.get("idempotency_key")
    if key and is_journaled(hermes_home, key):
        return path
    line = json.dumps(entry, ensure_ascii=True, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
    return path


def is_journaled(hermes_home: Any, key: Optional[str]) -> bool:
    """True when ``key`` already exists in the durable journal (idempotency check)."""
    if not key:
        return False
    return _journal_has_key(journal_path(hermes_home), key)


def _journal_has_key(path: Path, key: str) -> bool:
    """Best-effort scan of an existing journal for ``key`` (small file, linear ok)."""
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if existing.get("idempotency_key") == key:
                    return True
    except OSError:
        return False
    return False


def read_journal(hermes_home: Any) -> list:
    """Read every entry in the durable journal (newest last)."""
    path = journal_path(hermes_home)
    if not path.exists():
        return []
    entries = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries


def build_bounded_ack(entry: Dict[str, Any]) -> str:
    """Truthful same-thread acknowledgement for a request that outlived its deadline.

    This is a *truthful* ack, not a "still working" ping: it says the request is
    captured and tracked under a specific id, that the answer is being worked in
    the background, and where it will land. It deliberately does NOT claim a
    human has read anything.
    """
    tracking = entry.get("tracking_id", "?")
    deadline = entry.get("deadline_seconds", 0)
    return (
        "⏱️ Got it - this needs more than a quick answer, so I've captured it as "
        f"tracked request `{tracking}` instead of making you wait silently. "
        "I'll keep working on it in the background and post the result back here "
        f"when it's ready. (Turn was cut off at its {int(deadline)}s bound to avoid "
        "a long silent wait.)"
    )


def build_resume_reference(entry: Dict[str, Any]) -> str:
    """Short truthful reference for a resumed turn whose request was already acked.

    Used when a gateway restart re-runs the same interrupted turn and it hits the
    deadline again: we must NOT re-answer or send a duplicate ack, and must NOT ask
    the user to resend. This is a one-line status reference, not a new answer.
    """
    tracking = entry.get("tracking_id", "?")
    return (
        f"⏱️ Still on it - this is already tracked as `{tracking}` from your earlier "
        "message; I'll post the result back here when it's ready."
    )
