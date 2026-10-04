"""Progress-based watchdog: progress-aware max-runtime + stall detection + auto-checkpoints.

These tests pin the behaviour that replaces the crude wall-clock kill: a worker that is still
making durable progress (fresh heartbeat OR checkpoint) is never killed by the clock, while a
worker that has made no durable progress for ``progress_stall_seconds`` is reclaimed as a genuine
stall with the reason recorded.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def no_alive_pid(monkeypatch):
    """Pretend SIGTERM killed the worker immediately so the reclaim path exits fast."""
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)


def _claim_running(conn, *, max_runtime_seconds=None, backdate_seconds=0):
    """Create + claim + stamp a worker pid; backdate the active run if requested."""
    tid = kb.create_task(
        conn, title="job", assignee="worker", max_runtime_seconds=max_runtime_seconds,
    )
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    kbd._set_worker_pid(conn, tid, os.getpid())
    if backdate_seconds:
        started = int(time.time()) - backdate_seconds
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (started, tid))
            conn.execute(
                "UPDATE task_runs SET started_at = ? "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (started, tid),
            )
    return tid, claimed.current_run_id


def _fresh_checkpoint(conn, tid, run_id, progress=None):
    kb.save_task_checkpoint(
        conn, tid, expected_run_id=run_id, progress=progress if progress is not None else {"ok": True},
    )


# ---------------------------------------------------------------------------
# detect_stalled_workers (the PRIMARY progress judge)
# ---------------------------------------------------------------------------

def test_stall_kills_worker_with_no_durable_progress(kanban_home, no_alive_pid):
    with kbc.connect() as conn:
        tid, run_id = _claim_running(conn, backdate_seconds=30)

        stalled = kbd.detect_stalled_workers(
            conn, progress_stall_seconds=1, signal_fn=lambda pid, sig: None,
        )

        assert stalled == [tid]
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.worker_pid is None
        # The stall reason is recorded on the run and the event.
        events = kb.list_events(conn, tid)
        stall_events = [e for e in events if e.kind == "stalled"]
        assert len(stall_events) == 1
        assert stall_events[0].payload["progress_stall_seconds"] == 1
        runs = kb.list_runs(conn, tid)
        assert runs[-1].outcome == "stalled"
        assert "no progress" in (runs[-1].error or "")


def test_stall_spares_worker_with_fresh_heartbeat(kanban_home, no_alive_pid):
    with kbc.connect() as conn:
        tid, run_id = _claim_running(conn, backdate_seconds=30)
        conn.execute("UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?", (int(time.time()), tid))
        conn.commit()

        stalled = kbd.detect_stalled_workers(
            conn, progress_stall_seconds=5, signal_fn=lambda pid, sig: None,
        )

        assert stalled == []
        assert kb.get_task(conn, tid).status == "running"


def test_stall_spares_worker_with_fresh_checkpoint(kanban_home, no_alive_pid):
    with kbc.connect() as conn:
        tid, run_id = _claim_running(conn, backdate_seconds=30)
        _fresh_checkpoint(conn, tid, run_id)

        stalled = kbd.detect_stalled_workers(
            conn, progress_stall_seconds=5, signal_fn=lambda pid, sig: None,
        )

        assert stalled == []
        assert kb.get_task(conn, tid).status == "running"


def test_stall_disabled_when_progress_stall_seconds_is_zero(kanban_home):
    with kbc.connect() as conn:
        tid, run_id = _claim_running(conn, backdate_seconds=30)

        assert kbd.detect_stalled_workers(conn, progress_stall_seconds=0) == []
        assert kb.get_task(conn, tid).status == "running"


# ---------------------------------------------------------------------------
# enforce_max_runtime (wall-clock demoted to a progress-aware backstop)
# ---------------------------------------------------------------------------

def test_max_runtime_still_kills_unprogressed_overrun(kanban_home, no_alive_pid):
    """The existing contract: past the clock AND no progress -> timed_out (unchanged)."""
    with kbc.connect() as conn:
        tid, run_id = _claim_running(conn, max_runtime_seconds=1, backdate_seconds=30)

        timed_out = kbd.enforce_max_runtime(
            conn, progress_stall_seconds=5, signal_fn=lambda pid, sig: None,
        )

        assert timed_out == [tid]
        assert kb.get_task(conn, tid).status == "ready"


def test_max_runtime_spares_fresh_progress_worker(kanban_home, no_alive_pid):
    """Wall-clock must NOT kill a worker that is still leaving durable progress."""
    with kbc.connect() as conn:
        tid, run_id = _claim_running(conn, max_runtime_seconds=1, backdate_seconds=30)
        _fresh_checkpoint(conn, tid, run_id)

        timed_out = kbd.enforce_max_runtime(
            conn, progress_stall_seconds=5, signal_fn=lambda pid, sig: None,
        )

        assert timed_out == []
        assert kb.get_task(conn, tid).status == "running"


def test_max_runtime_spares_fresh_heartbeat_worker(kanban_home, no_alive_pid):
    with kbc.connect() as conn:
        tid, run_id = _claim_running(conn, max_runtime_seconds=1, backdate_seconds=30)
        conn.execute("UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?", (int(time.time()), tid))
        conn.commit()

        timed_out = kbd.enforce_max_runtime(
            conn, progress_stall_seconds=5, signal_fn=lambda pid, sig: None,
        )

        assert timed_out == []
        assert kb.get_task(conn, tid).status == "running"


# ---------------------------------------------------------------------------
# Auto-checkpoint bridge (harness writes a durable trail without a manual call)
# ---------------------------------------------------------------------------

def test_auto_checkpoint_bridge_writes_durable_row(kanban_home, monkeypatch):
    from tools import kanban_tools as kt

    with kbc.connect() as conn:
        tid, run_id = _claim_running(conn)
        # Pin the dispatcher worker identity the bridge reads from the env.
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        monkeypatch.setenv("HERMES_PROFILE", "test-worker")
        monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
        # Fresh rate-limit window so the call is not throttled by a prior test.
        monkeypatch.setattr(kt, "_auto_checkpoint_last_attempt", 0.0)

        assert kt.checkpoint_current_worker_from_env() is True

        latest = kb.load_task_checkpoint(conn, tid)
        assert latest is not None
        assert latest.progress == {"_auto": True}
        assert latest.run_id == run_id
