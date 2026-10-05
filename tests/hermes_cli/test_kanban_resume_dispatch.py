"""Tests for the "do it now" resume dispatch (t_b438d9fa).

A human "do it now" tap on a ``needs_input`` / ``capability``-parked card must
RESUME the paused worker with its half-done context warm, not spin up a fresh
cold ``work kanban task`` run (which is why "nothing happens" after Harry
clicks ready and a separate timer scolds him for not pressing in time).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _block_needs_input(conn, tid, reason="waiting on Harry"):
    assert kb.block_task(conn, tid, reason=reason, kind="needs_input")
    assert kb.get_task(conn, tid).status == "blocked"


def test_resume_context_fresh_task_is_not_resume(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fresh", assignee="worker")
        assert kbd.resume_context(conn, tid).is_resume is False


def test_resume_context_blocked_but_not_unblocked_is_not_resume(kanban_home):
    with kbc.connect() as conn:
        tid = _running_task(conn)
        _block_needs_input(conn, tid)
        # Parked but no human tap yet: still not a resume.
        assert kbd.resume_context(conn, tid).is_resume is False


def test_resume_context_unblocked_needs_input_is_resume(kanban_home):
    with kbc.connect() as conn:
        tid = _running_task(conn)
        _block_needs_input(conn, tid)
        assert kb.unblock_task(conn, tid)
        ctx = kbd.resume_context(conn, tid)
        assert ctx.is_resume is True
        # resume_of links the warm run to the blocked run it continues.
        assert ctx.resume_of is not None


def test_dispatch_once_resumes_warm_not_cold(kanban_home, all_assignees_spawnable):
    captured = {}

    def fake_spawn(task, workspace, board=None, resume_ctx=None):
        captured["resume_ctx"] = resume_ctx
        return 4242

    with kbc.connect() as conn:
        tid = _running_task(conn)
        _block_needs_input(conn, tid)
        assert kb.unblock_task(conn, tid)
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert any(tid == row[0] for row in res.spawned)
        assert captured["resume_ctx"] is not None
        assert captured["resume_ctx"].is_resume is True
        assert captured["resume_ctx"].resume_of is not None


def test_dispatch_once_cold_for_fresh_task(kanban_home, all_assignees_spawnable):
    captured = {}

    def fake_spawn(task, workspace, board=None, resume_ctx=None):
        captured["resume_ctx"] = resume_ctx
        return 4242

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="cold", assignee="worker")
        # Never parked on a human: the dispatcher cold-dispatches. Clear any
        # claim bookkeeping so the ready row is genuinely spawnable.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
                "worker_pid=NULL, current_run_id=NULL WHERE id=?", (tid,))
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert any(tid == row[0] for row in res.spawned)
        assert captured["resume_ctx"] is None or captured["resume_ctx"].is_resume is False


def test_worker_argv_resume_prompt(kanban_home):
    with kbc.connect() as conn:
        tid = _running_task(conn)
        task = kb.get_task(conn, tid)
        cold = " ".join(kbd._worker_argv(task, "worker", None, None))
        resume = " ".join(kbd._worker_argv(
            task, "worker", None, kbd.ResumeContext(is_resume=True, resume_of=7)))
        # Cold dispatch keeps the classic prompt.
        assert "work kanban task" in cold
        # Resume dispatch swaps in the warm-resume prompt and drops the cold one.
        assert "resume kanban task" in resume
        assert "work kanban task" not in resume
