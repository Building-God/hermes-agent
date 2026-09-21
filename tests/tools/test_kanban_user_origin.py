"""Authenticated gateway requests remain separate from editable Kanban card text."""

import json
import sqlite3
from contextvars import copy_context
from types import SimpleNamespace

import pytest


def test_origin_is_immutable_and_idempotency_is_request_scoped(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    from gateway.session_context import (
        bind_inbound_user_task_origin, clear_session_vars, get_user_task_origin, set_session_vars,
    )
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools.registry import registry
    import tools.kanban_tools  # register the real tool

    kb._INITIALIZED_PATHS.clear()
    kb.init_db(db_path)
    tokens = set_session_vars(platform="discord", chat_id="chat-1", message_id="msg-1")
    try:
        raw = "Write HERMES_FRONT_DOOR_OK followed by a newline; verify exact bytes."
        source = SimpleNamespace(platform="discord", chat_id="chat-1", user_id="harry", is_bot=False)
        bind_inbound_user_task_origin(
            SimpleNamespace(text="unadmitted", message_id="unadmitted", internal=False), source,
        )
        assert get_user_task_origin() is None
        event = SimpleNamespace(text=raw, message_id="msg-1", internal=False, _user_task_origin_admitted=True)
        bind_inbound_user_task_origin(event, source)
        assert get_user_task_origin().text == raw
        inherited_context = copy_context()

        created = json.loads(registry.dispatch("kanban_create", {
            "title": "Exact output", "body": "Write 19 bytes", "assignee": "worker",
            "idempotency_key": "same-message",
        }))
        assert created["ok"] is True, created
        task_id = created["task_id"]
        assert raw not in json.dumps(created)

        with kbc.connect_closing(db_path) as conn:
            origin = conn.execute("SELECT * FROM task_user_origins WHERE task_id = ?", (task_id,)).fetchone()
            assert (origin["platform"], origin["chat_id"], origin["message_id"], origin["user_id"], origin["text"]) == (
                "discord", "chat-1", "msg-1", "harry", raw,
            )
            assert kb.edit_task(conn, task_id, body="Write 21 bytes")
            context = kb.build_worker_context(conn, task_id)
            assert context.index(raw) < context.index("## Body (agent-authored)")
            assert kb.task_goal_text(conn, kb.get_task(conn, task_id)).index(raw) < kb.task_goal_text(conn, kb.get_task(conn, task_id)).index("Write 21 bytes")
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute("UPDATE task_user_origins SET text = 'changed' WHERE task_id = ?", (task_id,))
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute("DELETE FROM task_user_origins WHERE task_id = ?", (task_id,))

        replay = json.loads(registry.dispatch("kanban_create", {
            "title": "replay", "body": "ignored", "assignee": "worker", "idempotency_key": "same-message",
        }))
        assert replay["task_id"] == task_id

        # Archiving then hard-deleting purges immutable provenance atomically.
        # Replaying the exact transport message must create a real, fresh task.
        with kbc.connect_closing(db_path) as conn:
            assert kb.archive_task(conn, task_id)
            assert kb.delete_task(conn, task_id)
            assert kb.get_task(conn, task_id) is None
            assert conn.execute(
                "SELECT 1 FROM task_user_origins WHERE task_id = ?", (task_id,),
            ).fetchone() is None

        fresh = json.loads(registry.dispatch("kanban_create", {
            "title": "fresh replay", "body": "new card", "assignee": "worker",
            "idempotency_key": "fresh-same-message",
        }))
        assert fresh["ok"] is True, fresh
        assert fresh["task_id"] != task_id
        with kbc.connect_closing(db_path) as conn:
            assert kb.get_task(conn, fresh["task_id"]) is not None

        bind_inbound_user_task_origin(
            SimpleNamespace(text="different request", message_id="msg-new", internal=False, _user_task_origin_admitted=True), source,
        )
        refused = json.loads(registry.dispatch("kanban_create", {
            "title": "not replay", "body": "ignored", "assignee": "worker", "idempotency_key": "fresh-same-message",
        }))
        assert "different user request" in refused["error"]
        with kbc.connect_closing(db_path) as conn:
            # The task-row purge trigger protects direct maintenance deletes too.
            conn.execute("DELETE FROM tasks WHERE id = ?", (fresh["task_id"],))
            assert conn.execute(
                "SELECT 1 FROM task_user_origins WHERE task_id = ?", (fresh["task_id"],),
            ).fetchone() is None

        bind_inbound_user_task_origin(
            SimpleNamespace(text="self continuation", message_id="msg-2", internal=True), source,
        )
        assert get_user_task_origin() is None
    finally:
        clear_session_vars(tokens)
    assert get_user_task_origin() is None
    assert inherited_context.run(get_user_task_origin) is None
