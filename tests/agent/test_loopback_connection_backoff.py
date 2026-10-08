"""Loopback-gateway connection retry backoff (t_46b46ce7).

A transient connection error to a local gateway (the LiteLLM proxy at
http://localhost:4000) must retry with patience so a ~30-60s restart rebind reads as a
brief delay, not a raw "Connection error" surfaced to the user after 3 fast attempts.
Remote providers keep the fast base_delay=2.0 backoff so a genuinely-flaky upstream
still fails over to the fallback chain promptly.
"""

from unittest.mock import MagicMock

import pytest

from agent.turn_recovery import compute_error_backoff

# Opt out of the tests/agent conftest fast-backoff fixture: these tests assert the
# real loopback backoff values, not that a retry path reaches a no-op wait.
pytestmark = pytest.mark.real_retry_backoff


def _connection_error():
    """An APIConnectionError-shaped transport failure, without importing openai."""
    return type("APIConnectionError", (Exception,), {})()


def _agent():
    agent = MagicMock()
    from agent.status_output import StatusOutputMixin
    for name in ("_emit_diagnostic_wait", "_buffer_diagnostic_status"):
        setattr(agent, name, getattr(StatusOutputMixin, name).__get__(agent))
    agent._client_log_context.return_value = ""
    return agent


def _backoff(err, base_url):
    return compute_error_backoff(
        _agent(), err, retry_count=1, max_retries=3, is_rate_limited=False,
        is_zai_coding_overload=False, base_url=base_url, model="chat-brain",
    )


def test_loopback_connection_error_uses_long_backoff():
    wait = _backoff(_connection_error(), "http://localhost:4000")
    assert wait >= 10.0  # base_delay=10.0 for the loopback gateway


def test_loopback_127_0_0_1_also_uses_long_backoff():
    wait = _backoff(_connection_error(), "http://127.0.0.1:4000")
    assert wait >= 10.0


def test_remote_connection_error_keeps_fast_backoff():
    wait = _backoff(_connection_error(), "https://api.openai.com/v1")
    assert wait < 10.0  # base_delay=2.0 unchanged for remote providers


def test_loopback_non_connection_error_keeps_fast_backoff():
    wait = _backoff(Exception("boom"), "http://localhost:4000")
    assert wait < 10.0


def test_loopback_retry_cycle_covers_thirty_second_outage():
    """The loopback backoff over the retry cycle must span >= 30s (the minimum named
    rebind window), so the brain waits the gateway out instead of erroring early."""
    from agent.retry_utils import jittered_backoff
    from agent.turn_recovery import (
        _LOOPBACK_CONNECTION_BACKOFF_BASE,
        _LOOPBACK_CONNECTION_BACKOFF_MAX,
    )

    span = sum(
        jittered_backoff(
            i, base_delay=_LOOPBACK_CONNECTION_BACKOFF_BASE,
            max_delay=_LOOPBACK_CONNECTION_BACKOFF_MAX, jitter_ratio=0.0,
        )
        for i in (1, 2)
    )
    assert span >= 30.0
