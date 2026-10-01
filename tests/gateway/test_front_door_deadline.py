"""Tests for the bounded front-door turn deadline.

Reproduces, on the real gateway code path, the observed 2026-10-01 failure (a
Discord turn that stayed "active" making slow API/tool calls for 20+ minutes with
no response-ready event) and asserts the fix: a wall-clock deadline yields a
truthful bounded acknowledgement plus a durable, restart-safe, idempotent journal
entry - not a long silent turn, not a duplicate answer, and never a resend request.

The real-path tests drive ``GatewayTurnMixin._run_agent_front_door_deadline_result``
and ``GatewayTurnMixin._run_agent_await_turn_worker`` directly (with the reaper and
hard-interrupt monkeypatched) so they fail if the wiring in ``gateway/run_turn.py``
is removed, unlike a toy in-test re-implementation of the poll loop.
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from gateway.front_door_deadline import (  # noqa: E402
    DEFAULT_DEADLINE_SECONDS,
    JOURNAL_FILENAME,
    build_bounded_ack,
    build_resume_reference,
    idempotency_key,
    is_journaled,
    journal_path,
    journal_request,
    make_request_entry,
    read_journal,
    resolve_front_door_deadline,
    tracking_id,
)


@pytest.fixture()
def journal_dir(tmp_path):
    return tmp_path / "home"


class FakeAgent:
    """Minimal still-running agent stub for the deadline result path."""

    def get_activity_summary(self):
        return {"api_call_count": 22, "seconds_since_activity": 0.0}


# ── resolve_front_door_deadline ────────────────────────────────────────────
def test_resolve_defaults_disabled():
    assert resolve_front_door_deadline(None, None) == 0.0
    assert resolve_front_door_deadline(0, None) == 0.0
    assert resolve_front_door_deadline(-5, None) == 0.0
    assert DEFAULT_DEADLINE_SECONDS == 0.0


def test_resolve_env_wins_and_parses():
    assert resolve_front_door_deadline(120, "90") == 90.0
    assert resolve_front_door_deadline(120, None) == 120.0
    assert resolve_front_door_deadline("120", None) == 120.0


def test_resolve_invalid_treated_as_disabled():
    assert resolve_front_door_deadline("not-a-number", None) == 0.0
    assert resolve_front_door_deadline(None, "0") == 0.0
    assert resolve_front_door_deadline(None, "") == 0.0


# ── idempotency key + tracking id ──────────────────────────────────────────
def test_idempotency_key_stable_within_second():
    ts = 1790800627.5
    k1 = idempotency_key("discord", "1488868684830212276", ts)
    k2 = idempotency_key("discord", "1488868684830212276", ts + 0.4)
    assert k1 == k2  # same second -> same key


def test_idempotency_key_content_fallback_stable_across_restart():
    # No persisted timestamp -> the key must NOT fall back to the wall clock, or a
    # restarted/resumed turn would re-key and never dedupe. It derives from the
    # message content instead, so the same message yields the same key.
    k1 = idempotency_key("discord", "c1", 0.0, "why is work stuck?")
    k2 = idempotency_key("discord", "c1", None, "why is work stuck?")
    k3 = idempotency_key("discord", "c1", 0.0, "a different question")
    assert k1 == k2
    assert k1 != k3
    # A timestamped request keys on the timestamp, distinct from the content path.
    assert k1 != idempotency_key("discord", "c1", 1000.0, "why is work stuck?")


def test_tracking_id_stable_and_unique():
    t1 = tracking_id("discord", "c1", 1000.0)
    t2 = tracking_id("discord", "c1", 1000.0)
    t3 = tracking_id("discord", "c2", 1000.0)
    assert t1 == t2
    assert t1 != t3
    assert t1.startswith("fd-")


# ── durability + idempotency ───────────────────────────────────────────────
def test_journal_request_is_durable_and_idempotent(journal_dir):
    entry = make_request_entry(
        platform="discord", conversation_id="c1", session_key="s1",
        user="Harry", message="why is work stuck?", request_ts=1000.0,
        deadline_seconds=120, ack_ts=1120.0,
    )
    journal_request(journal_dir, entry)
    # A second append with the same key is a no-op (no duplicate answer).
    journal_request(journal_dir, entry)
    entries = read_journal(journal_dir)
    assert len(entries) == 1
    assert is_journaled(journal_dir, entry["idempotency_key"])
    assert journal_path(journal_dir).name == JOURNAL_FILENAME


def test_restart_survives_and_dedupes(journal_dir):
    # Simulate: request journaled, gateway "restarts", a fresh reader (no in-memory
    # state) sees the durable record and can tell the request was already tracked.
    entry = make_request_entry(
        platform="discord", conversation_id="c1", session_key="s1",
        user="Harry", message="what is the security pin?", request_ts=2000.0,
        deadline_seconds=120, ack_ts=2120.0,
    )
    journal_request(journal_dir, entry)
    entries = read_journal(journal_dir)
    assert len(entries) == 1
    assert entries[0]["conversation_id"] == "c1"
    assert entries[0]["request_ts"] == 2000.0
    assert is_journaled(journal_dir, entry["idempotency_key"]) is True


# ── ack truthfulness (never a resend request) ──────────────────────────────
def test_ack_is_truthful_and_does_not_ask_resend():
    entry = make_request_entry(
        platform="discord", conversation_id="c1", session_key="s1",
        user="Harry", message="why is work stuck?", request_ts=1000.0,
        deadline_seconds=120, ack_ts=1120.0,
    )
    entry["card_id"] = "t_owned"
    ack = build_bounded_ack(entry)
    assert entry["card_id"] in ack
    assert "resend" not in ack.lower()
    assert "try again" not in ack.lower()
    assert "owns it" in ack  # truthful: names the tracking id, claims no human receipt


def test_resume_reference_differs_and_no_resend():
    entry = make_request_entry(
        platform="discord", conversation_id="c1", session_key="s1",
        user="Harry", message="why is work stuck?", request_ts=1000.0,
        deadline_seconds=120, ack_ts=1120.0,
    )
    entry["card_id"] = "t_owned"
    ref = build_resume_reference(entry)
    assert entry["card_id"] in ref
    assert "resend" not in ref.lower()
    assert ref != build_bounded_ack(entry)


# ── real gateway code path: _run_agent_front_door_deadline_result ─────────
def _deadline_runner_harness(tmp_path, monkeypatch):
    """Patch gateway.run side effects and return (runner, worker, interrupts)."""
    import gateway.run as gateway_run
    monkeypatch.setattr("hermes_cli.kanban_operator.dispatch_owner", lambda *a,**k: "pilot")
    monkeypatch.setattr("gateway.front_door_deadline.settle_foreground_fence", lambda *a,**k: None)

    monkeypatch.setattr(gateway_run, "_hermes_home", str(tmp_path))
    interrupts = []
    monkeypatch.setattr(gateway_run, "request_hard_interrupt", lambda *a, **k: interrupts.append(k))
    monkeypatch.setattr(gateway_run, "_abandon_timed_out_gateway_turn", lambda *a, **k: None)

    from gateway.run_turn import GatewayTurnMixin

    runner = object.__new__(GatewayTurnMixin)
    worker = SimpleNamespace(
        task_id="test-task-123456789", process_baseline=frozenset(), worker_done=None,
        timeout_fired=None, cleanup_lock=None, is_current=lambda: True,
    )
    return runner, worker, interrupts


def _make_turn_ctx(*, message="Bullshit. What is this security pin?",
                   persist_ts=1790800627.0, conversation_id="1488868684830212276"):
    from gateway.turn_context import TurnContext

    return TurnContext(
        source=SimpleNamespace(platform="discord", chat_id=conversation_id,
                               user_name="Harry", user_id="u1"),
        session_key=f"agent:main:discord:dm:{conversation_id}",
        message=message,
        persist_user_timestamp=persist_ts,
        agent_holder=[FakeAgent()],
        result_holder=[{}],
        tools_holder=[[]],
    )


def test_deadline_result_bounded_ack_journal_interrupt(tmp_path, monkeypatch):
    """The real deadline result emits a truthful bounded ack, journals durably,
    and interrupts the still-running agent."""
    runner, worker, interrupts = _deadline_runner_harness(tmp_path, monkeypatch)
    turn_ctx = _make_turn_ctx()

    result = asyncio.run(
        runner._run_agent_front_door_deadline_result(worker, turn_ctx, 120.0)
    )

    assert result["front_door_deadline"] is True
    assert result["already_acked"] is False
    assert result["tracking_id"].startswith("fd-")
    assert "owns it" in result["final_response"]  # truthful bounded ack
    assert "resend" not in result["final_response"].lower()
    assert interrupts, "request_hard_interrupt was not called on the still-running agent"

    entries = read_journal(str(tmp_path))
    assert len(entries) == 1
    assert entries[0]["conversation_id"] == "1488868684830212276"
    assert entries[0]["status"] == "acknowledged"


def test_deadline_result_resume_dedupes_persisted_timestamp(tmp_path, monkeypatch):
    """A resumed turn that re-hits the deadline with the SAME persisted timestamp
    is recognised and returns the resume reference - no duplicate journal, no re-ack."""
    runner, worker, _ = _deadline_runner_harness(tmp_path, monkeypatch)

    first = asyncio.run(
        runner._run_agent_front_door_deadline_result(worker, _make_turn_ctx(), 120.0)
    )
    second = asyncio.run(
        runner._run_agent_front_door_deadline_result(worker, _make_turn_ctx(), 120.0)
    )

    assert first["already_acked"] is False
    assert second["already_acked"] is True  # resumed turn recognised
    assert second["front_door_deadline"] is True
    assert "Existing task" in second["final_response"]  # resume reference, not a re-ack
    assert second["tracking_id"] == first["tracking_id"]
    assert len(read_journal(str(tmp_path))) == 1  # no duplicate answer


def test_deadline_result_content_fallback_dedupes(tmp_path, monkeypatch):
    """When no timestamp was persisted, the key still dedupes across resume because
    it falls back to a content hash - NOT the wall clock."""
    runner, worker, _ = _deadline_runner_harness(tmp_path, monkeypatch)

    first = asyncio.run(
        runner._run_agent_front_door_deadline_result(
            worker, _make_turn_ctx(persist_ts=None), 120.0)
    )
    second = asyncio.run(
        runner._run_agent_front_door_deadline_result(
            worker, _make_turn_ctx(persist_ts=None), 120.0)
    )

    assert first["already_acked"] is False
    assert second["already_acked"] is True
    assert len(read_journal(str(tmp_path))) == 1


# ── real gateway code path: _run_agent_await_turn_worker ──────────────────
def test_await_turn_worker_fires_deadline_on_slow_provider(tmp_path, monkeypatch):
    """The real poll loop, with a never-completing executor task (slow provider) and a
    deadline > 0, returns the deadline result within a bounded wall-clock window and
    journals + interrupts - not 20+ minutes of silence."""
    runner, worker, interrupts = _deadline_runner_harness(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_AGENT_TURN_DEADLINE", "0.1")

    worker.agent_timeout = None  # skip the inactivity branch; only the deadline bounds this turn
    worker.agent_warning = None
    turn_ctx = _make_turn_ctx(persist_ts=3000.0)

    async def _drive():
        loop = asyncio.get_running_loop()
        worker.executor_task = loop.create_future()  # never completes: slow provider path
        interrupt_detected = asyncio.Event()
        interrupt_detected.set()
        start = time.monotonic()
        result = await runner._run_agent_await_turn_worker(
            worker, turn_ctx, interrupt_detected, None,
        )
        return result, time.monotonic() - start

    result, elapsed = asyncio.run(_drive())

    assert result["front_door_deadline"] is True
    assert result["already_acked"] is False
    assert "owns it" in result["final_response"]
    assert interrupts, "request_hard_interrupt was not called on the still-running agent"
    assert elapsed < 15.0  # bounded (the poll interval is the coarse upper bound), not minutes
    assert len(read_journal(str(tmp_path))) == 1
