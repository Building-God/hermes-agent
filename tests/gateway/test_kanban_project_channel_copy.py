"""Delivery Phase 2: project-channel untagged copies (t_06cd3273).

A task carrying a project tag (``project_id``) present in
``kanban.project_channel_map`` gets a second, untagged (no user_id) copy
delivered to the mapped project channel alongside the origin @-mention
delivery. An empty map delivers origin-only.
"""

import asyncio

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _create_completed_project_subscription(tmp_path, monkeypatch, project_id):
    db_path = tmp_path / "project-copy.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(
            conn, title="project copy", assignee="worker",
        )
        # create_task resolves project_id against projects.db and drops
        # unknown slugs; set the tag directly so the notifier sees it.
        conn.execute("UPDATE tasks SET project_id = ? WHERE id = ?", (project_id, tid))
        conn.commit()
        # Origin sub carries the @-mention driver (user_id).
        kbn.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            delivery_metadata={"user_id": "938599234989617222"},
        )
        kb.complete_task(conn, tid, summary="done")
        return tid
    finally:
        conn.close()


def test_mapped_project_sends_untagged_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_project_channel_map",
        lambda: {"sage": "project-chat-1"},
    )
    _create_completed_project_subscription(tmp_path, monkeypatch, project_id="sage")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    by_chat = {s["chat_id"]: s for s in adapter.sent}
    # Origin @-mention delivery still lands.
    assert "chat-1" in by_chat, "origin delivery must still land"
    assert by_chat["chat-1"]["metadata"].get("user_id") == "938599234989617222"
    # Untagged copy lands in the mapped project channel.
    assert "project-chat-1" in by_chat, "untagged copy must land in the project channel"
    assert "user_id" not in by_chat["project-chat-1"]["metadata"], (
        "project copy must carry no user_id (no @-mention)"
    )
    assert len(adapter.sent) == 2


def test_empty_map_sends_origin_only(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gateway.kanban_watchers._kanban_project_channel_map",
        lambda: {},
    )
    _create_completed_project_subscription(tmp_path, monkeypatch, project_id="sage")

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [s["chat_id"] for s in adapter.sent] == ["chat-1"]
