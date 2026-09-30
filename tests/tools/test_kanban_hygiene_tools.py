"""Board-hygiene orchestrator tools (kanban_archive / kanban_reassign).

Proves the acceptance for the board-hygiene fix:
  - the orchestrator has archive (with a one-line reason) and reassign tools,
  - a dispatched worker still cannot use them (the fail-closed fence stays),
  - archive records the reason on the ``archived`` event (close, not delete).
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def orchestrator_env(monkeypatch, tmp_path):
    """Simulate the orchestrator lane: an isolated board, NO worker identity."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="duplicate job", assignee="pilot")
        # park it so it is not running (archive fences running trees)
        kb.block_task(conn, tid, reason="blocked for hygiene test")
    finally:
        conn.close()
    return tid


def test_archive_and_reassign_are_orchestrator_only():
    from tools import kanban_tools as kt

    assert "kanban_archive" in kt._ORCHESTRATOR_TOOLS
    assert "kanban_reassign" in kt._ORCHESTRATOR_TOOLS


def test_archive_records_reason(orchestrator_env):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    tid = orchestrator_env
    out = json.loads(kt._handle_archive({"task_id": tid, "reason": "duplicate of t_other"}))
    assert out["ok"] is True
    assert out["status"] == "archived"

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "archived"
        events = [e for e in kb.list_events(conn, tid) if e.kind == "archived"]
        assert events, "an 'archived' event must be recorded"
        reason_payload = events[-1].payload or {}
        assert reason_payload.get("reason") == "duplicate of t_other"


def test_reassign_moves_to_real_profile(orchestrator_env):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    tid = orchestrator_env
    out = json.loads(kt._handle_reassign({"task_id": tid, "profile": "reviewer"}))
    assert out["ok"] is True
    assert out["assignee"] == "reviewer"

    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).assignee == "reviewer"


def test_worker_cannot_archive(monkeypatch, orchestrator_env):
    """A dispatched worker (HERMES_KANBAN_TASK set) must be refused the archive tool."""
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_00000000")
    out = json.loads(kt._handle_archive({"task_id": orchestrator_env, "reason": "nope"}))
    assert out.get("ok") is not True
    assert "orchestrator-only" in out.get("error", "")


def test_duplicate_body_filing_returns_existing_card(orchestrator_env):
    """Filing the same body twice (different titles) returns ONE card id."""
    from tools import kanban_tools as kt

    body = "Restart the gateway so the rate-limit fix goes live, then prove the cooldown."
    first = json.loads(kt._handle_create({
        "title": "Restart the gateway", "assignee": "pilot", "body": body,
    }))
    assert first["ok"] is True
    first_id = first["task_id"]

    # Same body, different title -> deduped to the first card, no second card minted.
    second = json.loads(kt._handle_create({
        "title": "Cherry-pick the rate-limit fix onto the live gateway", "assignee": "pilot",
        "body": body,
    }))
    assert second["task_id"] == first_id
    assert second.get("deduped_to") == first_id


def test_duplicate_title_filing_returns_existing_card(orchestrator_env):
    """Filing the same title twice returns ONE card id."""
    from tools import kanban_tools as kt

    first = json.loads(kt._handle_create({"title": "Unique job title", "assignee": "pilot"}))
    first_id = first["task_id"]
    second = json.loads(kt._handle_create({"title": "Unique job title", "assignee": "pilot"}))
    assert second["task_id"] == first_id
    assert second.get("deduped_to") == first_id


def test_cross_assignee_title_is_not_deduped(orchestrator_env):
    """The same title for a DIFFERENT assignee must NOT be suppressed."""
    from tools import kanban_tools as kt

    first = json.loads(kt._handle_create({"title": "Shared phrasing job", "assignee": "pilot"}))
    second = json.loads(kt._handle_create({"title": "Shared phrasing job", "assignee": "reviewer"}))
    assert second["task_id"] != first["task_id"]

