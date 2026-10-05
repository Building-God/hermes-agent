"""Event-driven kanban dispatcher: the notifier wakes the dispatcher immediately.

The dispatcher used to sleep a full ``dispatch_interval_seconds`` (default 60s)
between ticks, so a completed worker / tapped card / merged PR sat unwatched
until the next poll. These tests pin the two halves of the fix:

* ``_signal_dispatcher_wake`` / ``_sleep_between_ticks_or_wake`` - a wake
  interrupts the dispatcher's sleep so it ticks NOW, while an un-woken sleep
  still elapses (no tight-loop).
* the notifier calls ``_signal_dispatcher_wake`` when it claims a terminal
  event (completion / block / unblock / review), closing the loop from
  "event happened" to "dispatcher reacted" in the same gateway process.
"""

import asyncio
import time

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        event._gateway_accepted = True

    # Artifact-delivery stubs: a plain completion has none, but the notifier
    # probes these helpers defensively.
    async def send_multiple_images(self, chat_id, images, metadata=None):
        return None

    async def send_video(self, chat_id, video_path, metadata=None):
        return None

    async def send_document(self, chat_id, file_path, metadata=None):
        return None


def _bare_runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    return runner


def test_signal_dispatcher_wake_sets_event_and_is_idempotent():
    runner = _bare_runner()
    runner._signal_dispatcher_wake()
    runner._signal_dispatcher_wake()
    assert runner._dispatcher_wake_event().is_set()


def test_sleep_between_ticks_or_wake_returns_early_when_woken():
    runner = _bare_runner()

    async def main():
        async def wake_later():
            await asyncio.sleep(0.05)
            runner._signal_dispatcher_wake()

        asyncio.ensure_future(wake_later())
        started = time.monotonic()
        await runner._sleep_between_ticks_or_wake(60.0)
        elapsed = time.monotonic() - started
        # Woken well before the 60s interval (and before the first full 1s slice).
        assert elapsed < 0.9, f"expected an interruptible wake, slept {elapsed:.2f}s"
        # The edge is consumed so the next sleep isn't an instant re-wake.
        assert not runner._dispatcher_wake_event().is_set()

    asyncio.run(main())


def test_sleep_between_ticks_or_wake_sleeps_when_not_woken():
    runner = _bare_runner()

    async def main():
        started = time.monotonic()
        await runner._sleep_between_ticks_or_wake(1.0)
        elapsed = time.monotonic() - started
        # No wake: the full (floored-to-1s) interval must elapse, proving the
        # dispatcher does not tight-loop when idle.
        assert elapsed >= 0.9, f"expected a full slice, returned after {elapsed:.2f}s"

    asyncio.run(main())


def test_notifier_wakes_dispatcher_on_terminal_event(tmp_path, monkeypatch):
    db_path = tmp_path / "event-driven-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="wake on completion", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: RecordingAdapter()}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()

    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None  # the notifier's initial wiring delay
        runner._running = False
        await real_sleep(0)

    async def run_tick():
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        await runner._kanban_notifier_watcher(interval=1)

    asyncio.run(run_tick())

    # One completed terminal event was claimed -> the dispatcher was woken.
    assert runner._dispatcher_wake_event().is_set()
    assert len(runner.adapters[Platform.TELEGRAM].sent) == 1
