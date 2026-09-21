"""Max-runtime retries are fenced behind verified worker-tree termination."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def isolated_board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set both board paths before any connection is initialized."""
    home = tmp_path / ".hermes"
    home.mkdir()
    board = home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kbc.connect(board) as conn:
        yield conn


def _overdue_claim(conn, pid: int) -> str:
    task_id = kb.create_task(conn, title="tree", assignee="worker", max_runtime_seconds=1)
    kb.claim_task(conn, task_id)
    kbd._set_worker_pid(conn, task_id, pid)
    old = int(time.time()) - 60
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, task_id))
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (old, task_id),
        )
    return task_id


@pytest.mark.windows_only
def test_max_runtime_kills_live_worker_tree_before_retry(isolated_board):
    """Windows taskkill /T removes a live parent+child before the claim is released."""
    child_program = "import time; time.sleep(120)"
    parent_program = (
        "import subprocess, sys, time; "
        f"child=subprocess.Popen([sys.executable, '-c', {child_program!r}]); "
        "print(child.pid, flush=True); time.sleep(120)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_program], stdout=subprocess.PIPE, text=True,
    )
    child_pid = None
    parent_fingerprint = kbd._process_fingerprint(parent.pid)
    child_fingerprint = None
    try:
        child_pid_text = parent.stdout.readline().strip() if parent.stdout else ""
        assert child_pid_text.isdigit()
        child_pid = int(child_pid_text)
        child_fingerprint = kbd._process_fingerprint(child_pid)
        assert parent_fingerprint is not None and child_fingerprint is not None
        task_id = _overdue_claim(isolated_board, parent.pid)

        assert kbd.enforce_max_runtime(isolated_board) == [task_id]
        assert not kbd._worker_alive(parent.pid, parent_fingerprint)
        assert not kbd._worker_alive(child_pid, child_fingerprint)

        task = kb.get_task(isolated_board, task_id)
        assert task is not None
        event = next(event for event in kb.list_events(isolated_board, task_id) if event.kind == "timed_out")
        assert task.status == "ready" and task.worker_pid is None
        assert child_pid in event.payload["descendant_pids"]
        assert event.payload["surviving_descendant_pids"] == []
    finally:
        # A failed assertion must not reproduce the orphan this regression guards.
        # Each cleanup kill re-validates the exact process identity captured above.
        for pid, fingerprint in ((parent.pid, parent_fingerprint), (child_pid, child_fingerprint)):
            if pid is None or fingerprint is None or kbd._process_fingerprint(pid) != fingerprint:
                continue
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
        remaining = []
        for _ in range(20):
            remaining = [
                (pid, fingerprint) for pid, fingerprint in ((parent.pid, parent_fingerprint), (child_pid, child_fingerprint))
                if pid is not None and fingerprint is not None and kbd._process_fingerprint(pid) == fingerprint
            ]
            if not remaining:
                break
            time.sleep(0.1)
        assert not remaining



@pytest.mark.windows_only
def test_tree_kill_refuses_recycled_root_and_descendant_identities(monkeypatch):
    """Every Windows taskkill has a just-in-time fingerprint guard."""
    root, child = 71_001, 71_002
    root_fingerprint, child_fingerprint = "epoch|root", "epoch|child"
    calls = []
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(kbd, "_snapshot_worker_descendants", lambda pid: ({child: child_fingerprint}, True))
    monkeypatch.setattr(kbd, "_worker_alive", lambda pid, started_at: True)
    monkeypatch.setattr(kbd, "_poll_descendant_exit", lambda descendants: [child])
    monkeypatch.setattr(kbd.subprocess, "run", lambda argv, **kwargs: calls.append(argv) or type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())

    # The root is valid for the first check and recycled by the just-in-time one.
    fingerprints = iter((root_fingerprint, "epoch|other-root", "epoch|other-child"))
    monkeypatch.setattr(kbd, "_process_fingerprint", lambda pid: next(fingerprints))
    root_result = kbd._terminate_reclaimed_worker(root, f"{kb._host_prefix()}lock", started_at=root_fingerprint)
    assert root_result["pid_recycled"] is True
    assert calls == []

    # A valid root may be taskkilled, but a snapshotted child whose identity
    # changed after the snapshot must never receive a second taskkill.
    fingerprints = iter((root_fingerprint, root_fingerprint, "epoch|other-child"))
    monkeypatch.setattr(kbd, "_process_fingerprint", lambda pid: next(fingerprints))
    child_result = kbd._terminate_reclaimed_worker(root, f"{kb._host_prefix()}lock", started_at=root_fingerprint)
    assert child_result["surviving_descendant_pids"] == [child]
    assert [argv[2] for argv in calls] == [str(root)]


def test_tree_kill_holds_unreadable_live_descendant_without_signalling_it(monkeypatch):
    """A live child with an unreadable identity blocks release but is never killed by bare PID."""
    root, child = 71_101, 71_102
    calls = []
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kbd, "_pid_recycled", lambda _pid, _started_at: False)
    monkeypatch.setattr(kbd, "_snapshot_worker_descendants", lambda _pid: ({child: "epoch|child"}, True))
    monkeypatch.setattr(kbd, "_worker_alive", lambda _pid, _started_at: False)
    monkeypatch.setattr(kbd, "_poll_descendant_exit", lambda _descendants: [child])
    monkeypatch.setattr(kbd, "_process_fingerprint", lambda _pid: None)

    result = kbd._terminate_reclaimed_worker(
        root, f"{kb._host_prefix()}lock", started_at="epoch|root",
        signal_fn=lambda pid, sig: calls.append((pid, sig)),
    )

    assert result["terminated"] is False
    assert result["surviving_descendant_pids"] == [child]
    assert [pid for pid, _sig in calls] == [root]

def test_max_runtime_failed_kill_holds_claim_with_retryable_diagnostic(isolated_board, monkeypatch):
    """A no-op signal hook cannot release a claim beside the still-live worker."""
    task_id = _overdue_claim(isolated_board, os.getpid())
    monkeypatch.setattr(kbd, "_snapshot_worker_descendants", lambda pid: ({}, True))
    monkeypatch.setattr(kbd, "_poll_worker_exit", lambda pid, started_at: False)

    assert kbd.enforce_max_runtime(isolated_board, signal_fn=lambda pid, sig: None) == []
    task = kb.get_task(isolated_board, task_id)
    assert task is not None
    events = kb.list_events(isolated_board, task_id)
    deferred = next(event for event in events if event.kind == "reclaim_deferred")
    assert task.status == "running" and task.worker_pid == os.getpid()
    assert deferred.payload["reason"] == "max_runtime_worker_tree_alive"
    assert deferred.payload["termination_attempted"] is True
    assert all(event.kind != "timed_out" for event in events)
