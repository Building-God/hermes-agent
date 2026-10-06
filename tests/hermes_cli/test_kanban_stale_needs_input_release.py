"""Regression tests for the dispatcher's stale ``needs_input`` auto-release.

A worker that parks on ``kanban_block(kind='needs_input')`` stays ``blocked``
forever when the human never answers: the dash-side question expiry does not
release the kanban block. The dispatcher tick now ages an unanswered question
out - the card flips back to ``ready`` with a plain-English safe-default note
and the owning worker respawns from its checkpoint. A card answered before the
TTL is already out of ``blocked`` and is left untouched.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


TTL = 60


def _block_needs_input(conn, title: str) -> str:
    tid = kb.create_task(conn, title=title)
    assert kb.block_task(conn, tid, reason="need Harry's decision", kind="needs_input")
    assert kb.get_task(conn, tid).status == "blocked"
    return tid


def _backdate_blocked_event(conn, tid: str, created_at: int) -> None:
    conn.execute(
        "UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'blocked'",
        (created_at, tid),
    )
    conn.commit()


def _release_tick(conn, ttl: int = TTL):
    """One dispatcher tick with no spawning - the reclaim phase still runs."""
    return kbd.dispatch_once(conn, max_spawn=0, stale_needs_input_ttl_seconds=ttl)


def test_stale_needs_input_releases_to_ready_with_logged_default(kanban_home: Path) -> None:
    """Unanswered needs_input older than the TTL flips to ready with a note."""
    with kbc.connect() as conn:
        tid = _block_needs_input(conn, "unanswered question")
        _backdate_blocked_event(conn, tid, int(time.time()) - TTL - 10)

        result = _release_tick(conn)

        assert result.stale_needs_input_released == [tid]
        assert kb.get_task(conn, tid).status == "ready"
        assert "stale_needs_input_released" in [e.kind for e in kb.list_events(conn, tid)]
        comments = kb.list_comments(conn, tid)
        assert any(
            "safe default" in c.body and "do not re-ask" in c.body for c in comments
        ), [c.body for c in comments]


def test_answered_before_ttl_is_left_untouched(kanban_home: Path) -> None:
    """Harry's answer releases the block first; the stale release leaves it."""
    with kbc.connect() as conn:
        tid = _block_needs_input(conn, "answered question")
        _backdate_blocked_event(conn, tid, int(time.time()) - TTL - 10)
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"

        result = _release_tick(conn)

        assert result.stale_needs_input_released == []
        assert kb.get_task(conn, tid).status == "ready"
        assert "stale_needs_input_released" not in [e.kind for e in kb.list_events(conn, tid)]


def test_within_ttl_stays_blocked(kanban_home: Path) -> None:
    """A fresh needs_input block is not yet stale and stays blocked."""
    with kbc.connect() as conn:
        tid = _block_needs_input(conn, "fresh question")

        result = _release_tick(conn)

        assert result.stale_needs_input_released == []
        assert kb.get_task(conn, tid).status == "blocked"
        assert "stale_needs_input_released" not in [e.kind for e in kb.list_events(conn, tid)]


def test_ttl_zero_disables_release(kanban_home: Path) -> None:
    """``ttl_seconds <= 0`` (the config off switch) releases nothing."""
    with kbc.connect() as conn:
        tid = _block_needs_input(conn, "never-expiring question")
        _backdate_blocked_event(conn, tid, int(time.time()) - 10 * TTL)

        released = kb.release_stale_needs_input_blocks(conn, ttl_seconds=0)

        assert released == []
        assert kb.get_task(conn, tid).status == "blocked"
