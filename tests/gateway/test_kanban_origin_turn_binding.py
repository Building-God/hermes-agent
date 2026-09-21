"""A platform turn reaches the production origin binder before Kanban creation."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_platform_turn_binds_original_origin_after_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db_path = tmp_path / "board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    for name in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN", "HERMES_KANBAN_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)

    from evals.heartbeat_idle_wire import WireAdapter
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from gateway import session_context
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools.registry import registry
    import tools.kanban_tools  # noqa: F401  real tool registration

    clear_session_vars = session_context.clear_session_vars
    set_session_vars = session_context.set_session_vars
    # Before M20 there was no origin getter. Keep the same behavioural probe
    # runnable on that base so it fails at the missing turn binding, not import.
    get_user_task_origin = getattr(session_context, "get_user_task_origin", lambda: None)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db(db_path)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized_for_source = lambda source, **kwargs: source.user_id == "human"
    runner._admit_bot_message_for_source = lambda source: not source.is_bot
    runner._hm_pre_gateway_dispatch_hook = lambda event, source: replace(event, text="model-facing rewrite")
    runner._hmwa_open_session = AsyncMock(return_value=(False, False))
    runner._pinned_session_context_prompt = lambda *args: ""

    tokens = []
    lease_acquired = False
    created = []

    def bind_session(context):
        nonlocal tokens
        tokens = set_session_vars(
            platform="telegram", chat_id="chat", user_id="human", message_id="m1",
            session_key="test-session", session_id="test-session-id",
        )
        return tokens

    async def acquire_lease(*args):
        nonlocal lease_acquired
        assert get_user_task_origin() is None
        lease_acquired = True

    class StopAfterTool(Exception):
        pass

    async def mark_active_turn(event, session_key):
        assert lease_acquired
        origin = get_user_task_origin()
        assert origin is not None and origin.text == "original request"
        created.append(json.loads(registry.dispatch("kanban_create", {
            "title": "through real turn binder", "assignee": "worker", "idempotency_key": "m1",
        })))
        raise StopAfterTool

    runner._set_session_env = bind_session
    runner._hmwa_acquire_turn_lease = acquire_lease
    runner._mark_durable_active_turn = mark_active_turn

    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)

    async def handle(event):
        admitted = await runner._hm_admit_event(event)
        assert admitted is not None
        admitted_event, source, is_internal = admitted
        assert not is_internal
        entry = SimpleNamespace(
            session_key="test-session", session_id="test-session-id", created_at=0, updated_at=0,
        )
        try:
            with pytest.raises(StopAfterTool):
                await runner._hmwa_prepare_turn(
                    admitted_event, source, entry, entry.session_key, entry.session_key, 1,
                )
        finally:
            clear_session_vars(tokens)

    adapter.set_message_handler(handle)
    await adapter.connect()
    try:
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="human", chat_type="dm")
        await adapter.handle_message(MessageEvent(text="original request", source=source, message_id="m1"))
        while adapter._background_tasks:
            await asyncio.gather(*list(adapter._background_tasks))
    finally:
        await adapter.disconnect()

    assert len(created) == 1 and created[0]["ok"] is True
    with kbc.connect_closing(db_path) as conn:
        row = conn.execute(
            "SELECT platform, chat_id, message_id, user_id, text FROM task_user_origins WHERE task_id = ?",
            (created[0]["task_id"],),
        ).fetchone()
        assert tuple(row) == ("telegram", "chat", "m1", "human", "original request")
    assert get_user_task_origin() is None
