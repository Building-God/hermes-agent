"""Operator status must reflect preserved results without disclosing task prose."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from hermes_cli.kanban_status_readonly import build_snapshot, read_only_connection


@pytest.fixture
def board():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, assignee TEXT,
            completed_at INTEGER, result TEXT, created_at INTEGER,
            started_at INTEGER, last_heartbeat_at INTEGER, max_runtime_seconds INTEGER,
            current_run_id INTEGER);
        CREATE TABLE task_events (id INTEGER, task_id TEXT, kind TEXT, created_at INTEGER,
            payload TEXT);
        CREATE TABLE task_comments (task_id TEXT, body TEXT, created_at INTEGER);
        CREATE TABLE task_runs (id INTEGER, task_id TEXT, summary TEXT, outcome TEXT,
            started_at INTEGER, last_heartbeat_at INTEGER);
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
        CREATE TABLE task_attachments (task_id TEXT, filename TEXT, stored_path TEXT);
        CREATE TABLE kanban_notify_subs (task_id TEXT, last_event_id INTEGER,
            delivery_failures INTEGER, claim_token TEXT);
    """)
    try:
        yield conn
    finally:
        conn.close()


def _task(conn, task_id, *, status="running", result=None, completed_at=None):
    conn.execute("INSERT INTO tasks (id, title, status, assignee, completed_at, result, created_at) "
                 "VALUES (?, ?, ?, 'pilot', ?, ?, 1)",
                 (task_id, f"Safe {task_id}", status, completed_at, result))


def test_done_with_empty_task_result_still_has_preserved_result_evidence(board):
    _task(board, "t_done", status="done", completed_at=200)
    board.execute("INSERT INTO task_runs (id,task_id,summary,outcome) "
                  "VALUES (1, 't_done', 'PRIVATE RUN SUMMARY', 'completed')")
    board.execute("INSERT INTO task_attachments VALUES ('t_done', 'report.txt', '/private/path')")
    board.execute("INSERT INTO task_events VALUES (10, 't_done', 'completed', 199, 'PRIVATE PAYLOAD')")
    result = build_snapshot(board, board="fixture")["recent_results"][0]
    assert result["result_evidence"] == {
        "available": True, "sources": ["run_summary", "attachments"],
        "attachment_count": 1, "last_run_outcome": "completed",
    }
    rendered = json.dumps(result)
    assert "PRIVATE RUN SUMMARY" not in rendered
    assert "/private/path" not in rendered
    assert "PRIVATE PAYLOAD" not in rendered


def test_running_subscription_with_no_completion_is_not_delivered(board):
    _task(board, "t_running")
    board.execute("INSERT INTO kanban_notify_subs VALUES ('t_running', 0, 0, NULL)")
    item = build_snapshot(board, board="fixture")["open_tasks"][0]
    assert item["notification_cursor"] == "not_applicable"
    assert item["human_receipt"] == "unproven"


def test_completed_cursor_pending_then_settled_without_receipt_claim(board):
    _task(board, "t_done", status="done", completed_at=200)
    board.execute("INSERT INTO task_events VALUES (10, 't_done', 'completed', 199, NULL)")
    board.execute("INSERT INTO kanban_notify_subs VALUES ('t_done', 9, 0, NULL)")
    pending = build_snapshot(board, board="fixture")["recent_results"][0]
    assert pending["notification_cursor"] == "pending_or_retrying"
    board.execute("UPDATE kanban_notify_subs SET last_event_id=10 WHERE task_id='t_done'")
    settled = build_snapshot(board, board="fixture")["recent_results"][0]
    assert settled["notification_cursor"] == "cursor_settled_not_receipt"
    assert settled["human_receipt"] == "unproven"


def test_retry_remains_pending_even_with_advanced_cursor(board):
    _task(board, "t_done", status="done", completed_at=200)
    board.execute("INSERT INTO task_events VALUES (10, 't_done', 'completed', 199, NULL)")
    board.execute("INSERT INTO kanban_notify_subs VALUES ('t_done', 10, 1, NULL)")
    assert build_snapshot(board, board="fixture")["recent_results"][0]["notification_cursor"] == "pending_or_retrying"


def test_status_event_on_running_task_is_not_terminal_completion(board):
    _task(board, "t_running")
    board.execute("INSERT INTO task_events VALUES (10, 't_running', 'status', 199, NULL)")
    board.execute("INSERT INTO kanban_notify_subs VALUES ('t_running', 10, 0, NULL)")
    assert build_snapshot(board, board="fixture")["open_tasks"][0]["notification_cursor"] == "not_applicable"


def test_automatic_block_is_not_called_a_human_request(board):
    _task(board, "t_auto", status="blocked")
    board.execute("INSERT INTO task_events VALUES (10, 't_auto', 'gave_up', 199, '{}')")
    snapshot = build_snapshot(board, board="fixture")
    item = snapshot["open_tasks"][0]
    assert item["block_origin"] == "automatic_failure"
    assert item["human_action"] == "not_recorded"
    assert snapshot["summary"]["idle"] is True


def test_only_typed_needs_input_is_explicit_human_action(board):
    _task(board, "t_human", status="blocked")
    board.execute("INSERT INTO task_events VALUES (10, 't_human', 'blocked', 199, ?)",
                  ('{"kind":"needs_input","reason":"PRIVATE DETAIL"}',))
    item = build_snapshot(board, board="fixture")["open_tasks"][0]
    assert item["block_origin"] == "needs_input"
    assert item["human_action"] == "explicitly_requested"
    assert "PRIVATE DETAIL" not in json.dumps(item)


def test_todo_names_blocked_prerequisite_without_inventing_human_action(board):
    from hermes_cli.kanban_status_readonly import render_status

    _task(board, "t_parent", status="blocked")
    _task(board, "t_child", status="todo")
    board.execute("INSERT INTO task_links VALUES ('t_parent','t_child')")
    board.execute("INSERT INTO task_events VALUES (10,'t_parent','gave_up',199,'{}')")
    snapshot = build_snapshot(board, board="fixture")
    child = next(task for task in snapshot["open_tasks"] if task["id"] == "t_child")
    assert child["open_prerequisites"] == {
        "tasks": [{"id": "t_parent", "status": "blocked",
                   "block_origin": "automatic_failure", "human_action": "not_recorded"}],
        "truncated": False,
    }
    rendered = render_status(snapshot)
    assert "Open prerequisites: t_parent [blocked, automatic_failure, human action: not_recorded]" in rendered


def test_todo_propagates_only_typed_human_prerequisite(board):
    _task(board, "t_parent", status="blocked")
    _task(board, "t_child", status="todo")
    board.execute("INSERT INTO task_links VALUES ('t_parent','t_child')")
    board.execute("INSERT INTO task_events VALUES (10,'t_parent','blocked',199,?)",
                  ('{"kind":"needs_input","reason":"PRIVATE ASK"}',))
    child = next(task for task in build_snapshot(board, board="fixture")["open_tasks"]
                 if task["id"] == "t_child")
    parent = child["open_prerequisites"]["tasks"][0]
    assert parent["block_origin"] == "needs_input"
    assert parent["human_action"] == "explicitly_requested"
    assert "PRIVATE ASK" not in json.dumps(child)


def test_running_separates_heartbeat_log_activity_and_runtime_cap(board, monkeypatch):
    from hermes_cli import kanban_status_readonly as status_module

    _task(board, "t_running")
    board.execute("UPDATE tasks SET started_at=100, last_heartbeat_at=130, current_run_id=1, "
                  "max_runtime_seconds=300 WHERE id='t_running'")
    board.execute("INSERT INTO task_runs (id,task_id,started_at,last_heartbeat_at) "
                  "VALUES (1,'t_running',100,130)")
    monkeypatch.setattr(status_module, "_worker_log_activity", lambda *args: {
        "last_write_at": "1970-01-01T00:02:00Z", "size_bytes": 40,
    })
    item = build_snapshot(board, board="fixture")["open_tasks"][0]
    assert item["worker_liveness"] == {
        "last_heartbeat_at": "1970-01-01T00:02:10Z",
        "log": {"last_write_at": "1970-01-01T00:02:00Z", "size_bytes": 40},
        "runtime_cap_at": "1970-01-01T00:06:40Z",
    }


def test_resumed_running_uses_active_attempt_not_first_start(board, monkeypatch):
    from hermes_cli import kanban_status_readonly as status_module

    _task(board, "t_resumed")
    board.execute("UPDATE tasks SET started_at=100, last_heartbeat_at=130, "
                  "max_runtime_seconds=300, current_run_id=2 WHERE id='t_resumed'")
    board.execute("INSERT INTO task_runs (id,task_id,started_at,last_heartbeat_at) "
                  "VALUES (1,'t_resumed',100,130), (2,'t_resumed',500,510)")
    seen = []
    monkeypatch.setattr(status_module, "_worker_log_activity", lambda *args: (
        seen.append(args) or {"last_write_at": None, "size_bytes": None}
    ))
    live = build_snapshot(board, board="fixture")["open_tasks"][0]["worker_liveness"]
    assert live["last_heartbeat_at"] == "1970-01-01T00:08:30Z"
    assert live["runtime_cap_at"] == "1970-01-01T00:13:20Z"
    assert seen == [("t_resumed", "fixture", 500)]


def test_worker_log_activity_excludes_previous_attempt(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_status_readonly as status_module

    log_path = tmp_path / "worker.log"
    log_path.write_bytes(b"old")
    os.utime(log_path, (100, 100))
    monkeypatch.setattr(kb, "worker_log_path", lambda *args, **kwargs: log_path)
    assert status_module._worker_log_activity("t_resumed", "fixture", 500) == {
        "last_write_at": None, "size_bytes": None,
    }
    os.utime(log_path, (520, 520))
    assert status_module._worker_log_activity("t_resumed", "fixture", 500) == {
        "last_write_at": "1970-01-01T00:08:40Z", "size_bytes": 3,
    }


def test_idle_board_and_bounded_results(board):
    for index in range(12):
        _task(board, f"t_{index}", status="done", completed_at=index + 1)
    snapshot = build_snapshot(board, board="fixture")
    assert snapshot["summary"] == {
        "open": 0, "done": 12, "blocked": 0, "running": 0, "ready": 0, "idle": True,
        "open_shown": 0, "open_truncated": False,
    }
    assert len(snapshot["recent_results"]) == 10
    assert snapshot["recent_results"][0]["id"] == "t_11"
    with pytest.raises(ValueError):
        build_snapshot(board, board="fixture", completed_limit=21)


def test_open_tasks_are_bounded_and_truncation_is_visible(board):
    for index in range(4):
        _task(board, f"t_{index}")
    snapshot = build_snapshot(board, board="fixture", open_limit=2)
    assert snapshot["summary"]["open"] == 4
    assert snapshot["summary"]["open_shown"] == 2
    assert snapshot["summary"]["open_truncated"] is True
    assert len(snapshot["open_tasks"]) == 2
    with pytest.raises(ValueError):
        build_snapshot(board, board="fixture", open_limit=101)


def test_read_only_connection_does_not_create_missing_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    with pytest.raises(sqlite3.OperationalError):
        read_only_connection(board="missing-status-board")
    assert not list(tmp_path.rglob("*.db"))


def test_slash_status_does_not_initialize_a_missing_board(tmp_path, monkeypatch):
    from hermes_cli.kanban import run_slash

    missing = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(missing))
    output = run_slash("status")
    assert "unable to open database file" in output.lower()
    assert not missing.exists()


def test_status_tool_uses_read_only_board_and_rejects_mutation(tmp_path, monkeypatch):
    path = tmp_path / "kanban.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, assignee TEXT,
                completed_at INTEGER, result TEXT, created_at INTEGER,
                started_at INTEGER, last_heartbeat_at INTEGER, max_runtime_seconds INTEGER,
                current_run_id INTEGER);
            CREATE TABLE task_events (id INTEGER, task_id TEXT, kind TEXT, created_at INTEGER);
            CREATE TABLE task_comments (task_id TEXT, body TEXT, created_at INTEGER);
            CREATE TABLE task_runs (id INTEGER, task_id TEXT, summary TEXT, outcome TEXT,
                started_at INTEGER, last_heartbeat_at INTEGER);
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
            CREATE TABLE task_attachments (task_id TEXT, filename TEXT);
            CREATE TABLE kanban_notify_subs (task_id TEXT, last_event_id INTEGER,
                delivery_failures INTEGER, claim_token TEXT);
            INSERT INTO tasks (id, title, status, assignee, completed_at, result, created_at)
                VALUES ('t_1', 'Safe title', 'done', 'pilot', 2, NULL, 1);
            INSERT INTO task_runs (id,task_id,summary,outcome)
                VALUES (1, 't_1', 'PRIVATE SUMMARY', 'completed');
            INSERT INTO task_events VALUES (1, 't_1', 'completed', 2);
        """)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    import tools.kanban_tools  # noqa: F401 - registers the tool
    from tools.registry import registry

    raw = registry.dispatch("kanban_status", {"board": "default"})
    payload = json.loads(raw)
    assert payload["recent_results"][0]["result_evidence"]["sources"] == ["run_summary"]
    assert "PRIVATE SUMMARY" not in raw
    with read_only_connection(board="default") as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE tasks SET title='changed' WHERE id='t_1'")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT title FROM tasks WHERE id='t_1'").fetchone()[0] == "Safe title"


def test_slash_status_formats_bounded_result_without_model_turn(monkeypatch):
    from hermes_cli import kanban_status_readonly as status_module
    from hermes_cli.kanban import run_slash

    seen = {}

    def snapshot(board, *, open_limit, completed_limit):
        seen.update(board=board, open_limit=open_limit, completed_limit=completed_limit)
        return {
            "board": "fixture", "summary": {
                "open": 3, "done": 1, "blocked": 0, "idle": False,
                "open_shown": 1, "open_truncated": True,
            },
            "open_tasks": [{"id": "t_open", "title": "Safe current task", "status": "running",
                            "assignee": "pilot", "last_checkpoint_at": "2026-09-21T00:00:00Z"}],
            "recent_results": [{"id": "t_done", "title": "Safe completed task",
                                "result_evidence": {"sources": ["run_summary", "attachments"],
                                                    "attachment_count": 2},
                                "notification_cursor": "no_subscriber"}],
        }

    monkeypatch.setattr(status_module, "snapshot_for_board", snapshot)
    output = run_slash("status --open-limit 1 --completed-limit 1")
    assert seen == {"board": None, "open_limit": 1, "completed_limit": 1}
    assert "Showing 1 of 3 open tasks" in output
    assert "t_done" in output and "run_summary+attachments" in output
    assert "not proof of message delivery" in output


def test_snapshot_resolves_each_profile_home_without_cross_home_leak(tmp_path, monkeypatch):
    from hermes_cli.kanban_status_readonly import snapshot_for_board

    schema = """
        CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, assignee TEXT,
            completed_at INTEGER, result TEXT, created_at INTEGER,
            started_at INTEGER, last_heartbeat_at INTEGER, max_runtime_seconds INTEGER,
            current_run_id INTEGER);
        CREATE TABLE task_events (id INTEGER, task_id TEXT, kind TEXT, created_at INTEGER, payload TEXT);
        CREATE TABLE task_comments (task_id TEXT, body TEXT, created_at INTEGER);
        CREATE TABLE task_runs (id INTEGER, task_id TEXT, summary TEXT, outcome TEXT,
            started_at INTEGER, last_heartbeat_at INTEGER);
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
        CREATE TABLE task_attachments (task_id TEXT, filename TEXT);
        CREATE TABLE kanban_notify_subs (task_id TEXT, last_event_id INTEGER,
            delivery_failures INTEGER, claim_token TEXT);
    """
    homes = [tmp_path / "a", tmp_path / "b"]
    for home, task_id in zip(homes, ("t_a", "t_b")):
        home.mkdir()
        with sqlite3.connect(home / "kanban.db") as conn:
            conn.executescript(schema)
            conn.execute(
                "INSERT INTO tasks (id,title,status,assignee,created_at) VALUES (?, ?, 'done', 'pilot', 1)",
                (task_id, f"Title {task_id}"),
            )
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    for home, expected in ((homes[0], "t_a"), (homes[1], "t_b"), (homes[0], "t_a")):
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert [row["id"] for row in snapshot_for_board("default")["recent_results"]] == [expected]
