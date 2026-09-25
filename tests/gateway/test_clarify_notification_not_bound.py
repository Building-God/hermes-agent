"""Regression tests for kanban t_fb1c5819: a job notification must never answer a clarify.

The gateway clarify-binding path (``GatewayRunner._hm_clarify_reply``) used to accept
ANY inbound text as the answer to a pending clarify. When the dash Jarvis called
``clarify`` and then a kanban notifier wake ("[kanban] Task t_... blocked", delivered
as a synthetic ``MessageEvent(internal=True)``) landed on the same session, the wake was
bound as ``user_response`` - a system notification impersonated Harry's consent, and a
needs_input card was unblocked as if he had replied.

Two attribution guards now close that hole:

1. ``event.internal`` (system/notification traffic) never resolves a pending clarify.
2. notification-shaped text ("[kanban]", "[gateway]", automatic task-status lines) is
   rejected by ``clarify_gateway.attempt_text_response_for_session`` and leaves the
   clarify armed.

A real Harry message still resolves it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _clear_clarify_state():
    from tools import clarify_gateway as cm

    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _source() -> SessionSource:
    return SessionSource(
        platform=MagicMock(value="a2a"),
        chat_id="dash-thread-1",
        chat_type="dm",
        user_id="jarvis-dash",
    )


def _event(text: str, *, internal: bool = False) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="msg1",
        internal=internal,
    )


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._pending_event_audio_paths = lambda event: []
    runner._delivery_adapter_for = lambda source: None
    return runner


@pytest.mark.asyncio
async def test_internal_kanban_wake_does_not_answer_clarify():
    """Acceptance 1: a pending clarify stays unanswered when a kanban auto-notification
    arrives as the next inbound message - no user_response consumed."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    sk = "session:default/dash-thread-1"
    cm.register("c-notify", sk, "How to unblock the scout cards?", None)
    runner = _runner()

    wake_text = (
        "[kanban] Task t_9d02f55d blocked; needs attention.\n"
        "Title: How we all talk to each other - the living doctrine\n"
        "Assignee: @architect\nBoard: hermes-takeover\n\n"
        "This is an automatic task-status notification, not a request to decompose the task again."
    )
    result = await runner._hm_clarify_reply(_event(wake_text, internal=True), _source(), sk)

    # Falls through (not consumed as an answer) - the clarify remains pending.
    assert result is None
    pending = cm.get_pending_for_session(sk, include_choice_prompts=True)
    assert pending is not None
    assert pending.clarify_id == "c-notify"
    assert not pending.event.is_set()


@pytest.mark.asyncio
async def test_non_internal_notification_text_does_not_answer_clarify():
    """A relayed notification that arrives on a non-internal path (e.g. a bridge
    re-posting a kanban line) is still excluded by its text shape."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    sk = "session:default/dash-thread-1"
    cm.register("c-notify2", sk, "How to unblock the scout cards?", None)
    runner = _runner()

    wake_text = (
        "[gateway] Task t_x blocked; needs attention.\n"
        "This is an automatic task-status notification, not a request to decompose the task again."
    )
    result = await runner._hm_clarify_reply(_event(wake_text, internal=False), _source(), sk)

    assert result is None
    pending = cm.get_pending_for_session(sk, include_choice_prompts=True)
    assert pending is not None
    assert not pending.event.is_set()


@pytest.mark.asyncio
async def test_real_harry_answer_resolves_clarify():
    """Acceptance 2: a real (non-internal, non-notification) message binds and the
    answer is attributed to Harry."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    sk = "session:default/dash-thread-1"
    cm.register("c-real", sk, "How to unblock the scout cards?", None)
    runner = _runner()

    result = await runner._hm_clarify_reply(
        _event("Trigger the scout-driver tick now", internal=False), _source(), sk,
    )

    assert result == ""  # resolved: adapters don't double-post, agent continues
    pending = cm.get_pending_for_session(sk, include_choice_prompts=True)
    assert pending is not None
    assert pending.event.is_set()
    assert pending.response == "Trigger the scout-driver tick now"
