"""Gateway admission preserves origin only when pre-dispatch keeps the same principal."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from evals.heartbeat_idle_wire import WireAdapter
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_context import bind_inbound_user_task_origin, get_user_task_origin


def test_source_rewrite_revokes_user_task_origin_receipt():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized_for_source = lambda source, **kwargs: True
    runner._admit_bot_message_for_source = lambda source: True

    original = SessionSource(platform=Platform.DISCORD, chat_id="chat", user_id="harry")
    rewritten = SessionSource(platform=Platform.DISCORD, chat_id="chat", user_id="other")
    event = MessageEvent(text="exact request", source=original, message_id="message")
    runner._hm_pre_gateway_dispatch_hook = lambda event, source: replace(event, text="rewritten", source=rewritten)

    admitted = asyncio.run(runner._hm_admit_event(event))

    assert admitted is not None
    admitted_event, _, is_internal = admitted
    assert is_internal is False
    assert admitted_event._user_task_origin_text is None
    assert admitted_event._user_task_origin_source is None
    assert admitted_event._user_task_origin_admitted is False


@pytest.mark.asyncio
async def test_platform_admission_binds_original_request_for_real_kanban_create(tmp_path, monkeypatch):
    """A real adapter delivery reaches gateway admission before the real tool dispatch."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools.registry import registry
    import tools.kanban_tools  # register real kanban_create

    db_path = tmp_path / "board.db"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(db_path)

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized_for_source = lambda source, **kwargs: source.user_id == "human"
    runner._admit_bot_message_for_source = lambda source: not source.is_bot
    runner._hm_pre_gateway_dispatch_hook = lambda event, source: replace(event, text="model rewrite")
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    created = []

    async def gateway_turn(event):
        admitted = await runner._hm_admit_event(event)
        if admitted is None:
            return None
        admitted_event, source, _ = admitted
        bind_inbound_user_task_origin(admitted_event, source)
        origin = get_user_task_origin()
        if origin is not None:
            created.append(json.loads(registry.dispatch("kanban_create", {
                "title": "from platform", "assignee": "worker", "idempotency_key": event.message_id,
            })))
        return None

    adapter.set_message_handler(gateway_turn)
    await adapter.connect()
    try:
        human = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="human", chat_type="dm")
        await adapter.handle_message(MessageEvent(text="original request", source=human, message_id="m1"))
        while adapter._background_tasks:
            await asyncio.gather(*list(adapter._background_tasks))
        assert created and created[0]["ok"] is True
        with kbc.connect_closing(db_path) as conn:
            origin = conn.execute("SELECT text FROM task_user_origins WHERE task_id = ?", (created[0]["task_id"],)).fetchone()
            assert origin["text"] == "original request"

        for source, message_id in (
            (SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="intruder", chat_type="dm"), "m2"),
            (SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="human", is_bot=True, chat_type="dm"), "m3"),
        ):
            await adapter.handle_message(MessageEvent(text="must not persist", source=source, message_id=message_id))
        while adapter._background_tasks:
            await asyncio.gather(*list(adapter._background_tasks))
        assert len(created) == 1
    finally:
        await adapter.disconnect()
