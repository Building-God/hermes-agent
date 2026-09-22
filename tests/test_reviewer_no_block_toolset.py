"""Reviewer profile must be structurally incapable of emitting kanban_block.

Contract test for t_f8c942b9. The regression: an independent review CHILD card's
reviewer first tried ``kanban_request_changes`` (refused: "active run was not
claimed from review"), then reached for ``kanban_block(kind=needs_input)`` for
internal review ambiguity. R5 projected that block onto the Harry question
surface. Harry rejected being asked technical questions the agents can solve.

The fix is a narrow reviewer Kanban toolset (``kanban_reviewer``) that keeps the
verdict + evidence tools (show / status / comment / complete / request_review /
request_changes / heartbeat / attach / attach_url / attachments / checkpoint)
and OMITS ``kanban_block``, ``kanban_unblock``, ``kanban_create``,
``kanban_link``. A reviewer whose ``platform_toolsets.cli`` is
``[terminal, file_readonly, kanban_reviewer]`` cannot emit a block at all —
the tool is not in its model-visible schema.

Runs in-process against a temp HERMES_HOME (autouse ``_isolate_hermes_home``
in ``tests/conftest.py``). No subprocess, no provider I/O, no live board
mutation, no synthetic Harry question / Discord send. Also asserts the
shipped reviewer profile config (if present at ``profiles/reviewer/config.yaml``)
lists ``kanban_reviewer`` and NOT ``kanban`` under ``platform_toolsets.cli``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


REVIEWER_TOOLSET_KEY = "kanban_reviewer"

# Tools the reviewer MUST retain: verdict, evidence, liveness, artifact staging.
_REVIEWER_REQUIRED = {
    "kanban_show", "kanban_list", "kanban_status",
    "kanban_complete", "kanban_request_review", "kanban_request_changes",
    "kanban_heartbeat", "kanban_comment",
    "kanban_attach", "kanban_attach_url", "kanban_attachments",
}

# Tools the reviewer MUST NOT carry — these are the routes by which internal
# review ambiguity became a Harry question in t_f8c942b9, or orchestrator-only
# fan-out tools not appropriate to a reviewer.
_REVIEWER_FORBIDDEN = {
    "kanban_block",     # THE regression: needs_input for technical review ambiguity
    "kanban_unblock",   # inverse of block; orchestrator-only
    "kanban_create",    # fan-out; if a decision card is needed, escalation flow authors it
    "kanban_link",      # dependency wiring; orchestrator-only
}


_REVIEWER_LIKE_CONFIG = textwrap.dedent(
    """\
    model:
      default: claude-opus-4-7
      provider: anthropic
    agent:
      max_turns: 20
    toolsets:
      - terminal
      - file_readonly
      - kanban_reviewer
    platform_toolsets:
      cli:
        - terminal
        - file_readonly
        - kanban_reviewer
    """
)


@pytest.fixture
def reviewer_like_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a reviewer-shaped config; cache-busted."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    (home / "config.yaml").write_text(_REVIEWER_LIKE_CONFIG, encoding="utf-8")

    from hermes_cli import config as cfg_module
    cfg_module._LOAD_CONFIG_CACHE.clear()
    cfg_module._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    import toolsets
    toolsets._resolve_toolset_memo.clear()
    return home


def test_reviewer_toolset_static_membership() -> None:
    """The mechanism itself: kanban_reviewer excludes block/unblock/create/link."""
    from toolsets import TOOLSETS, resolve_toolset

    assert REVIEWER_TOOLSET_KEY in TOOLSETS, (
        "toolsets.py is missing the kanban_reviewer toolset registration"
    )
    resolved = set(resolve_toolset(REVIEWER_TOOLSET_KEY))

    missing = _REVIEWER_REQUIRED - resolved
    assert not missing, (
        f"kanban_reviewer must expose verdict/evidence tools; missing: {sorted(missing)}"
    )

    forbidden_present = resolved & _REVIEWER_FORBIDDEN
    assert not forbidden_present, (
        f"kanban_reviewer must NOT expose {sorted(forbidden_present)} — that is the "
        f"regression channel from t_f8c942b9 (kanban_block(kind=needs_input) projected "
        f"onto the Harry question surface)."
    )


def test_reviewer_like_cli_resolves_without_block(reviewer_like_home) -> None:
    """A reviewer-shaped config resolves to a CLI tool set that excludes kanban_block."""
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import resolve_toolset

    config = load_config()
    assert config.get("platform_toolsets", {}).get("cli") == [
        "terminal", "file_readonly", "kanban_reviewer"
    ], "fixture config did not round-trip through load_config"

    enabled = _get_platform_tools(config, "cli", include_default_mcp_servers=False)
    assert REVIEWER_TOOLSET_KEY in enabled, (
        f"CLI toolset resolution dropped kanban_reviewer; got {sorted(enabled)}"
    )
    assert "kanban" not in enabled, (
        f"CLI resolution unexpectedly re-enabled the full 'kanban' toolset "
        f"(which carries kanban_block); got {sorted(enabled)}"
    )

    # Union the static membership of every enabled toolset — the actual
    # model-visible tool names for a reviewer CLI session.
    resolved_tools: set[str] = set()
    for ts in enabled:
        resolved_tools.update(resolve_toolset(ts))

    forbidden_present = resolved_tools & _REVIEWER_FORBIDDEN
    assert not forbidden_present, (
        f"Reviewer CLI session resolved to include forbidden tools "
        f"{sorted(forbidden_present)}. Full tool set: {sorted(resolved_tools)}"
    )
    missing = _REVIEWER_REQUIRED - resolved_tools
    assert not missing, (
        f"Reviewer CLI session lacks required tools {sorted(missing)}; "
        f"got {sorted(resolved_tools)}"
    )


def test_reviewer_toolset_registered_in_configurable_list() -> None:
    """``hermes tools`` must surface kanban_reviewer for opt-in/inspection."""
    from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS

    keys = {k for (k, _l, _d) in CONFIGURABLE_TOOLSETS}
    assert REVIEWER_TOOLSET_KEY in keys, (
        f"kanban_reviewer missing from CONFIGURABLE_TOOLSETS; without registration "
        f"``hermes tools --summary`` and the checklist cannot show or toggle it. "
        f"Registered keys: {sorted(keys)}"
    )


def test_shipped_reviewer_profile_uses_kanban_reviewer() -> None:
    """The shipped reviewer profile config (if present in a colocated profiles/ tree)
    lists kanban_reviewer under platform_toolsets.cli and never the full kanban.

    Skips cleanly if the repo does not ship a ``profiles/reviewer/`` template, so
    this test remains a strict runtime contract check on other checkouts.
    """
    template = Path(__file__).resolve().parents[1] / "profiles" / "reviewer" / "config.yaml"
    if not template.exists():
        pytest.skip(f"no shipped reviewer template at {template}")
    import yaml
    parsed = yaml.safe_load(template.read_text(encoding="utf-8")) or {}
    cli_toolsets = ((parsed.get("platform_toolsets") or {}).get("cli")) or []
    assert REVIEWER_TOOLSET_KEY in cli_toolsets, (
        f"reviewer template config.yaml lacks kanban_reviewer on cli platform_toolsets; "
        f"got {cli_toolsets}"
    )
    assert "kanban" not in cli_toolsets, (
        f"reviewer template config.yaml still enables the full 'kanban' toolset "
        f"(which includes kanban_block); got {cli_toolsets}"
    )


def test_independent_child_reject_uses_only_permitted_tools(reviewer_like_home) -> None:
    """Simulate the independent-child rejection path: comment the parent, complete
    this child as rejected. Every tool the flow needs is in the reviewer's schema;
    no forbidden tool is required. This is the workflow that previously reached for
    kanban_block(kind=needs_input); with kanban_reviewer it structurally cannot.
    """
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import resolve_toolset

    config = load_config()
    enabled = _get_platform_tools(config, "cli", include_default_mcp_servers=False)
    resolved_tools: set[str] = set()
    for ts in enabled:
        resolved_tools.update(resolve_toolset(ts))

    # The independent-child reject sequence: comment on parent, then complete this
    # child card with review_outcome=rejected. Both tools must be reachable.
    reject_flow = {"kanban_show", "kanban_comment", "kanban_complete"}
    missing_for_reject = reject_flow - resolved_tools
    assert not missing_for_reject, (
        f"Reviewer cannot execute the independent-child reject flow; "
        f"missing tools: {sorted(missing_for_reject)}"
    )

    # And the tool the regression reached for is not there.
    assert "kanban_block" not in resolved_tools, (
        "kanban_block reachable to reviewer — the regression channel from t_f8c942b9 "
        "is open again. A reviewer must not be able to project internal review "
        "ambiguity onto the Harry question surface."
    )
