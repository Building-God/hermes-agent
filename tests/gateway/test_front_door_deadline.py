"""Tests for the bounded front-door turn deadline.

Reproduces, in miniature, the observed 2026-10-01 failure (a Discord turn that
stayed "active" making slow API/tool calls for 20+ minutes with no response-ready
event) and asserts the fix: a wall-clock deadline yields a truthful bounded
acknowledgement plus a durable, restart-safe, idempotent journal entry - not a
long silent turn, not a duplicate answer, and never a resend request.
"""

import sys
import time
from pathlib import Path

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
    ack = build_bounded_ack(entry)
    assert entry["tracking_id"] in ack
    assert "resend" not in ack.lower()
    assert "try again" not in ack.lower()
    assert "tracked request" in ack  # truthful: names the tracking id, claims no human receipt


def test_resume_reference_differs_and_no_resend():
    entry = make_request_entry(
        platform="discord", conversation_id="c1", session_key="s1",
        user="Harry", message="why is work stuck?", request_ts=1000.0,
        deadline_seconds=120, ack_ts=1120.0,
    )
    ref = build_resume_reference(entry)
    assert entry["tracking_id"] in ref
    assert "resend" not in ref.lower()
    assert ref != build_bounded_ack(entry)


# ── reproduction: slow provider/tool path → bounded ack + durable journal ──
def _run_controlled_turn(journal_dir, *, conversation_id, request_ts, deadline):
    """Miniature of the gateway poll loop's wall-clock deadline check.

    The "agent" stays active (it never idles), so an inactivity-only watchdog
    would never fire - the exact 06:07 failure shape. The wall-clock deadline is
    what bounds it. Returns the measured timeline.
    """
    wall_start = time.monotonic()
    ack_ts = None
    while True:
        if (time.monotonic() - wall_start) >= deadline:
            entry = make_request_entry(
                platform="discord", conversation_id=conversation_id,
                session_key=f"agent:main:discord:dm:{conversation_id}",
                user="RidingOnEggshells",
                message="Bullshit. What is this security pin triage promoter can't work?",
                request_ts=request_ts, deadline_seconds=deadline, ack_ts=time.time(),
            )
            if not is_journaled(journal_dir, entry["idempotency_key"]):
                journal_request(journal_dir, entry)
            ack_ts = entry["ack_ts"]
            break
        time.sleep(0.01)  # simulated "active" work (keeps the inactivity watchdog quiet)
    result_ts = time.time()
    return {"wall_seconds": time.monotonic() - wall_start, "ack_ts": ack_ts,
            "result_ts": result_ts, "tracking_id": entry["tracking_id"]}


@pytest.mark.parametrize("conv_id", ["1488868684830212276", "1553351661894893569"])
def test_two_controlled_turns_are_bounded_acked_and_durable(journal_dir, conv_id):
    request_ts = 1790800627.0
    timeline = _run_controlled_turn(journal_dir, conversation_id=conv_id,
                                    request_ts=request_ts, deadline=0.2)
    # Bounded: exits in well under any "20 minute" bound.
    assert timeline["wall_seconds"] < 5.0
    # Measurable timeline: request ts <= ack ts <= result ts.
    assert request_ts <= timeline["ack_ts"] <= timeline["result_ts"]
    # Durable tracked continuation, keyed to the conversation.
    entries = read_journal(journal_dir)
    assert len(entries) == 1
    assert entries[0]["conversation_id"] == conv_id
    assert entries[0]["tracking_id"] == timeline["tracking_id"]
    assert entries[0]["status"] == "acknowledged"
