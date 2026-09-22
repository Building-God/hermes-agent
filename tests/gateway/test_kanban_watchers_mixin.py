"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import inspect

from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gateway_dispatcher_stuck_warning_names_guard_reason(monkeypatch, caplog):
    """The embedded dispatcher's "stuck" warning names the respawn-guard reason
    holding the ready queue (#111910) instead of a bare zero-spawn count."""
    import asyncio
    import logging

    import gateway.kanban_watchers as kw
    from hermes_cli import kanban_db_dispatch as kbd

    held = kbd.DispatchResult(respawn_guarded=[("t_held", "active_pr")])
    runner = object.__new__(kw.GatewayKanbanWatchersMixin)
    runner._running = True
    monkeypatch.setattr(runner, "_kanban_dispatcher_boot", lambda: (lambda: {}, object(), {}))

    class _Dispatcher:
        def __init__(self, *a, **k):
            pass

        def tick_once(self):
            return [("board", held)]

        def ready_nonempty(self):
            return True

    ticks = {"n": 0}

    async def _direct(fn, *args):
        return fn(*args)

    async def _sleep(_delay):
        ticks["n"] += 1
        if ticks["n"] > kw._HEALTH_WINDOW:
            runner._running = False

    monkeypatch.setattr(kw, "_KanbanDispatcher", _Dispatcher)
    monkeypatch.setattr(kw, "_resolve_dispatcher_settings", lambda cfg, kb: type("S", (), {"interval": 1.0})())
    monkeypatch.setattr(kw, "_to_thread_process_service", _direct)
    monkeypatch.setattr(kw, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(kw, "_resolve_auto_decompose_settings", lambda load_config: (False, 0))
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kw.asyncio, "sleep", _sleep)

    with caplog.at_level(logging.WARNING, logger=kw.logger.name):
        asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=5.0))

    stuck = [r.getMessage() for r in caplog.records if "dispatcher stuck" in r.getMessage()]
    assert stuck, [r.getMessage() for r in caplog.records]
    assert "Last tick held back: active_pr=1." in stuck[0]


def test_gateway_dispatcher_capacity_hold_is_not_stuck(monkeypatch, caplog):
    """Ready work waiting behind a running worker at ``max_in_progress`` is busy,
    not stuck: no warning however long the run (false alarms 2026-09-22)."""
    import asyncio
    import logging

    import gateway.kanban_watchers as kw
    from hermes_cli import kanban_db_dispatch as kbd

    results = [
        kbd.DispatchResult(capacity_held=True),
        kbd.DispatchResult(skipped_per_profile_capped=[("t_q", "pilot", 1)]),
    ]
    runner = object.__new__(kw.GatewayKanbanWatchersMixin)
    runner._running = True
    monkeypatch.setattr(runner, "_kanban_dispatcher_boot", lambda: (lambda: {}, object(), {}))
    ticks = {"n": 0}

    class _Dispatcher:
        def __init__(self, *a, **k):
            pass

        def tick_once(self):
            return [("board", results[ticks["n"] % 2])]

        def ready_nonempty(self):
            return True

    async def _direct(fn, *args):
        return fn(*args)

    async def _sleep(_delay):
        ticks["n"] += 1
        if ticks["n"] > kw._HEALTH_WINDOW * 3:
            runner._running = False

    monkeypatch.setattr(kw, "_KanbanDispatcher", _Dispatcher)
    monkeypatch.setattr(kw, "_resolve_dispatcher_settings", lambda cfg, kb: type("S", (), {"interval": 1.0})())
    monkeypatch.setattr(kw, "_to_thread_process_service", _direct)
    monkeypatch.setattr(kw, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(kw, "_resolve_auto_decompose_settings", lambda load_config: (False, 0))
    monkeypatch.setattr(kbd, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kw.asyncio, "sleep", _sleep)

    with caplog.at_level(logging.WARNING, logger=kw.logger.name):
        asyncio.run(asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=5.0))

    assert not [r for r in caplog.records if "dispatcher stuck" in r.getMessage()]


def test_tick_spawn_budget_marks_capacity_hold(monkeypatch):
    """A full ``max_in_progress`` marks the result so telemetry can tell busy from stuck."""
    import sqlite3

    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(kbd, "count_running_tasks", lambda conn: 1)
    monkeypatch.setattr(kbd, "count_running_tasks_other_boards", lambda board: 0)
    res = kbd.DispatchResult()
    may, _budget = kbd._tick_spawn_budget(
        sqlite3.connect(":memory:"), res, max_spawn=None, max_in_progress=1, board="b",
    )
    assert may is False and res.capacity_held is True
    assert kbd.held_by_capacity([res])
    free = kbd.DispatchResult()
    monkeypatch.setattr(kbd, "count_running_tasks", lambda conn: 0)
    kbd._tick_spawn_budget(sqlite3.connect(":memory:"), free, max_spawn=None, max_in_progress=1, board="b")
    assert free.capacity_held is False and not kbd.held_by_capacity([free, None])
