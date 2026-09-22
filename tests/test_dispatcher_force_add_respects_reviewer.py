"""Dispatcher force-add of the full 'kanban' toolset must not defeat a
reviewer profile that already opts into kanban_reviewer.

Regression test for t_ac1771f3.  Parent card t_f8c942b9 narrowed the reviewer
profile to ``[terminal, file_readonly, kanban_reviewer]`` so kanban_block /
kanban_create / kanban_link / kanban_unblock are not in the model-visible
schema.  Independent reviewer t_6709ac39 rejected that fix: at runtime
``model_tools._select_tool_names`` unconditionally appends the full ``kanban``
toolset whenever the literal string ``"kanban"`` is absent from ``enabled``
AND ``HERMES_KANBAN_TASK`` is set for a dispatcher-owned worker.  That
force-add re-exposed every forbidden tool to the reviewer's session.

This test drives ``_select_tool_names`` under the real dispatcher env
(``HERMES_KANBAN_TASK`` set, delegated marker absent) with the reviewer's
enabled toolsets, and asserts:

* the forbidden tools are absent from the resulting model-visible name set;
* the reviewer's required verdict/evidence/heartbeat tools remain present;
* a full-kanban worker (no kanban_reviewer opt-in) still receives the full
  kanban surface via the force-add — the narrowing is opt-in, not global.
"""

from __future__ import annotations

import pytest


_REVIEWER_REQUIRED = {
    "kanban_show", "kanban_list", "kanban_status",
    "kanban_complete", "kanban_request_review", "kanban_request_changes",
    "kanban_heartbeat", "kanban_comment",
    "kanban_attach", "kanban_attach_url", "kanban_attachments",
}
_REVIEWER_FORBIDDEN = {
    "kanban_block", "kanban_unblock", "kanban_create", "kanban_link",
}


@pytest.fixture
def dispatcher_env(monkeypatch):
    """Simulate a dispatcher-spawned worker: HERMES_KANBAN_TASK set, not a
    delegate_task child, and dispatcher-owned per delegation_context."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test_reviewer_bypass")
    import model_tools
    # Force the two context predicates to the dispatcher-owned worker shape
    # without depending on delegation_context internals.
    monkeypatch.setattr(model_tools, "_is_delegated_child_context", lambda: False)
    monkeypatch.setattr(model_tools, "_is_dispatcher_owned_worker", lambda: True)
    return None


def test_reviewer_toolset_survives_dispatcher_force_add(dispatcher_env):
    """The exact runtime path that let kanban_block through in t_ac1771f3."""
    from model_tools import _select_tool_names

    enabled = ["terminal", "file_readonly", "kanban_reviewer"]
    tools = _select_tool_names(enabled, None, quiet_mode=True)

    forbidden_present = tools & _REVIEWER_FORBIDDEN
    assert not forbidden_present, (
        f"Dispatcher force-add re-exposed forbidden reviewer tools "
        f"{sorted(forbidden_present)} despite kanban_reviewer opt-in. "
        f"This is the t_ac1771f3 regression channel."
    )

    missing_required = _REVIEWER_REQUIRED - tools
    assert not missing_required, (
        f"Reviewer lost required verdict/evidence tools: {sorted(missing_required)}"
    )


def test_full_kanban_worker_still_gets_force_add(dispatcher_env):
    """Preserve orchestrator/full-kanban behaviour: a worker whose enabled
    list carries neither 'kanban' nor a kanban_* variant still gets the
    lifecycle handoff tools force-added — that's the whole point of the
    dispatcher force-add.  Without this, an assignee profile that forgot
    to list 'kanban' would silently fail heartbeat/complete."""
    from model_tools import _select_tool_names

    enabled = ["terminal"]  # no kanban surface at all
    tools = _select_tool_names(enabled, None, quiet_mode=True)

    # The full kanban toolset should have been appended: at minimum
    # kanban_complete + kanban_heartbeat must be present.
    assert "kanban_complete" in tools, (
        f"dispatcher force-add stopped firing for a bare worker; got {sorted(t for t in tools if t.startswith('kanban_'))}"
    )
    assert "kanban_block" in tools, (
        "full-kanban worker lost kanban_block — the force-add is over-narrowed"
    )


def test_pilot_orchestrator_full_kanban_preserved(dispatcher_env):
    """A pilot/orchestrator worker that explicitly enables 'kanban' keeps every
    orchestrator tool (create/link/block/unblock)."""
    from model_tools import _select_tool_names

    enabled = ["terminal", "file_readonly", "kanban"]
    tools = _select_tool_names(enabled, None, quiet_mode=True)

    for orch_tool in ("kanban_create", "kanban_link", "kanban_block", "kanban_unblock"):
        assert orch_tool in tools, (
            f"orchestrator lost {orch_tool}; got {sorted(t for t in tools if t.startswith('kanban_'))}"
        )
