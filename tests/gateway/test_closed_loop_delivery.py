"""Delivery outage drills: durable route, independent text/file cursors and receipts."""
import asyncio
from types import SimpleNamespace
import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.kanban_watchers_notifier import _KanbanNotification, _Collector
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "board.db"))
    conn = kbc.connect()
    tid = kb.create_task(conn, title="Result", assignee="pilot",
                         user_origin={"platform":"discord", "chat_id":"origin", "message_id":"asked",
                                      "user_id":"harry", "text":"Requested outcome"})
    notify.add_notify_sub(conn, task_id=tid, platform="discord", chat_id="origin", thread_id="same-thread")
    kb.complete_task(conn, tid, summary="Measured outcome")
    event = kb.list_events(conn, tid)[-1]
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._kanban_sub_fail_counts = {}
    class Adapter:
        calls = 0
        async def send(self, chat_id, text, metadata=None):
            self.calls += 1
            assert chat_id == "origin" and metadata["thread_id"] == "same-thread"
            return SimpleNamespace(success=True, message_id="sent-result")
    adapter = Adapter()
    yield conn, tid, event, runner, adapter
    conn.close()


def notification(conn, tid, event, runner, adapter):
    sub = notify.list_notify_subs(conn, tid)[0]
    n = _KanbanNotification(runner, {"sub":sub, "task":kb.get_task(conn, tid), "events":[event],
                                   "cursor":event.id, "old_cursor":0},
                            platform_cls=Platform, sub_fail_counts={})
    n.adapter = adapter
    return n


def test_file_outage_retries_artifact_without_repeating_sent_text(delivery):
    conn, tid, event, runner, adapter = delivery
    async def upload(**kwargs):
        raise RuntimeError("controlled attachment outage")
    runner._deliver_kanban_artifacts = upload
    first = notification(conn, tid, event, runner, adapter)
    with pytest.raises(RuntimeError):
        asyncio.run(first._send_event(event, "Measured outcome"))
    assert adapter.calls == 1
    assert notify.list_notify_subs(conn, tid)[0]["last_ping_event_id"] == event.id
    assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='result_delivered'", (tid,)).fetchone()
    async def recovered(**kwargs):
        return [{"sha256":"measured", "size":21}]
    runner._deliver_kanban_artifacts = recovered
    second = notification(conn, tid, event, runner, adapter)
    assert asyncio.run(second._send_event(event, "Measured outcome"))
    assert adapter.calls == 1
    receipt = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='result_delivered'", (tid,)).fetchone()[0]
    assert '"human_receipt": "unobserved"' in receipt


def test_repeated_transport_failure_retains_route_and_durable_backoff(delivery):
    conn, tid, event, runner, adapter = delivery
    n = notification(conn, tid, event, runner, adapter)
    for _ in range(5):
        asyncio.run(n.delivery_failed("test failure %s/%s %d/%d %s", (tid,"discord"), "unused", RuntimeError("outage"), False))
    sub = notify.list_notify_subs(conn, tid)[0]
    assert sub["delivery_failures"] == 5 and sub["retry_after"] > 0
    collector = _Collector.__new__(_Collector)
    assert collector._claim_for_sub(conn, None, sub) is None


def test_send_failure_does_not_record_delivery(delivery):
    conn, tid, event, runner, adapter = delivery
    async def failed(*args, **kwargs):
        return SimpleNamespace(success=False, error="transport down")
    adapter.send = failed
    n = notification(conn, tid, event, runner, adapter)
    with pytest.raises(RuntimeError):
        asyncio.run(n._send_event(event, "result"))
    assert notify.list_notify_subs(conn, tid)[0]["last_ping_event_id"] == 0


def test_receipt_requires_exact_result_reply_and_authored_user(delivery):
    conn, tid, event, runner, adapter = delivery
    notify.record_notify_ping(conn, task_id=tid, platform="discord", chat_id="origin", thread_id="same-thread",
                               event_id=event.id, message_id="sent-result")
    params = dict(platform="discord", chat_id="origin", thread_id="same-thread", user_id="harry",
                  message_id="reply", reply_to_message_id="sent-result")
    assert notify.record_result_reply(conn, **(params | {"internal": True})) == []
    assert notify.record_result_reply(conn, **(params | {"user_id":"bot"})) == []
    assert notify.record_result_reply(conn, **(params | {"reply_to_message_id":"unrelated"})) == []
    assert notify.record_result_reply(conn, **params) == [tid]
    assert notify.record_result_reply(conn, **params) == [tid]
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='result_receipt'", (tid,)).fetchone()[0] == 1
