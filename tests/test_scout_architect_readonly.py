"""Scout / Architect profiles must expose read-only file access only.

Lane 0.4d contract test. The ``file_readonly`` toolset (`toolsets.py`) is the
mechanism, and its checklist row (`hermes_cli/tools_config.CONFIGURABLE_TOOLSETS`)
is what the ``hermes tools --summary`` renderer shows. This test drives BOTH:

- ``resolve_toolset("file_readonly")`` yields ``{read_file, search_files}`` and
  never contains ``write_file`` or ``patch``.
- A scout- / architect-shaped ``config.yaml`` (``platform_toolsets.cli =
  [file_readonly, kanban, session_search]``) resolves to a CLI toolset set
  that includes ``file_readonly`` and NEVER ``file``.
- The union of tools those enabled toolsets expand to -- the actual
  model-visible tool names for that session -- excludes ``write_file`` /
  ``patch`` while retaining ``read_file`` / ``search_files``.
- ``hermes tools --summary`` renders the read-only label for that config
  and its output contains no ``write_file`` / ``patch`` tokens.

Runs in-process against a temp HERMES_HOME (autouse ``_isolate_hermes_home``
in ``tests/conftest.py``). No subprocess to the real user profile store;
no ``HERMES_STATE_DB_GUARD_BYPASS``; no provider or network I/O.
"""

from __future__ import annotations

import io
import re
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

import pytest


# Word-boundary match for either write-capable file tool name -- matches the
# card's acceptance regex, using Python's ``re`` (no POSIX char classes).
_FORBIDDEN = re.compile(r"(?<![A-Za-z0-9_])(write_file|patch)(?![A-Za-z0-9_])")


_SCOUT_LIKE_CONFIG = textwrap.dedent(
    """\
    model:
      default: claude-opus-4-7
      provider: anthropic
    agent:
      max_turns: 20
    toolsets:
      - file_readonly
      - kanban
      - session_search
    platform_toolsets:
      cli:
        - file_readonly
        - kanban
        - session_search
    """
)


@pytest.fixture
def scout_like_home(tmp_path, monkeypatch):
    """Populate the isolated HERMES_HOME with a scout/architect-shaped config
    and reset the config-load cache so the test observes the file just
    written (not a cache entry from an earlier test)."""
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    (home / "config.yaml").write_text(_SCOUT_LIKE_CONFIG, encoding="utf-8")

    # Bust cached config + toolset resolution so the write is observed.
    from hermes_cli import config as cfg_module
    cfg_module._LOAD_CONFIG_CACHE.clear()
    cfg_module._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    import toolsets
    toolsets._resolve_toolset_memo.clear()
    return home


def test_file_readonly_toolset_static_membership() -> None:
    """The mechanism itself: file_readonly resolves to read + search only."""
    from toolsets import TOOLSETS, resolve_toolset

    assert "file_readonly" in TOOLSETS, (
        "toolsets.py is missing the file_readonly toolset registration"
    )
    resolved = set(resolve_toolset("file_readonly"))
    assert "read_file" in resolved and "search_files" in resolved
    forbidden = resolved & {"write_file", "patch"}
    assert not forbidden, (
        f"file_readonly toolset must not include write-capable tools; "
        f"found: {sorted(forbidden)}"
    )


def test_scout_like_cli_platform_resolves_readonly_only(scout_like_home) -> None:
    """Effective CLI tool-resolution path on a scout/architect-shaped config
    excludes ``file`` and includes ``file_readonly``; expanded tool names
    exclude the write-capable file tools."""
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import resolve_toolset

    config = load_config()
    assert config.get("platform_toolsets", {}).get("cli") == [
        "file_readonly", "kanban", "session_search"
    ], "fixture config did not round-trip through load_config"

    enabled = _get_platform_tools(config, "cli", include_default_mcp_servers=False)
    assert "file_readonly" in enabled, (
        f"CLI toolset resolution dropped file_readonly; got {sorted(enabled)}"
    )
    assert "file" not in enabled, (
        f"CLI resolution unexpectedly re-enabled the write-capable 'file' "
        f"toolset; got {sorted(enabled)}"
    )

    # Union the static membership of every enabled toolset (include_registry
    # is left at default so plugin-registered tools within a toolset are
    # counted too -- that's what a real session sees).
    resolved_tools: set[str] = set()
    for ts in enabled:
        resolved_tools.update(resolve_toolset(ts))

    assert "read_file" in resolved_tools, (
        f"file_readonly failed to contribute read_file; tools={sorted(resolved_tools)}"
    )
    assert "search_files" in resolved_tools, (
        f"file_readonly failed to contribute search_files; tools={sorted(resolved_tools)}"
    )
    forbidden = resolved_tools & {"write_file", "patch"}
    assert not forbidden, (
        f"Scout/Architect CLI session resolved to include write-capable file "
        f"tools {sorted(forbidden)}; full tool set: {sorted(resolved_tools)}"
    )


def test_tools_summary_shows_readonly_and_no_write_tokens(
    scout_like_home, monkeypatch
) -> None:
    """``hermes tools --summary`` renderer, driven in-process, shows the
    read-only file toolset label and emits no write_file / patch tokens."""
    from hermes_cli.config import load_config
    from hermes_cli import tools_config

    # Pin the enabled-platform set to CLI so the summary is deterministic
    # regardless of which messaging tokens happen to be in the ambient env.
    monkeypatch.setattr(tools_config, "_get_enabled_platforms", lambda: ["cli"])

    config = load_config()
    buf = io.StringIO()
    with redirect_stdout(buf):
        tools_config._print_tools_summary(config, ["cli"])
    output = buf.getvalue()

    # Positive: the read-only file toolset row is visible (label or key).
    assert "file (read-only)" in output.lower() or "file_readonly" in output.lower(), (
        f"tools --summary did not surface the read-only file toolset. Full output:\n{output}"
    )
    # Negative: no write-capable file tool tokens leak into the summary.
    match = _FORBIDDEN.search(output)
    assert match is None, (
        f"tools --summary output contains forbidden write-capable token "
        f"{match.group(0)!r}. Full output:\n{output}"
    )
    # The composite 'file' toolset must NOT be listed as enabled here.
    assert not re.search(r"(?<![A-Za-z_])file(?![A-Za-z_])(?!_readonly)", output.lower()) or \
        "file (read-only)" in output.lower(), (
        f"tools --summary suggests the write-capable 'file' toolset is enabled. "
        f"Full output:\n{output}"
    )


@pytest.mark.parametrize("profile", ["scout", "architect"])
def test_shipped_profile_configs_use_file_readonly(profile: str) -> None:
    """The two shipped profile-template configs in the repo (if present) list
    ``file_readonly`` under ``platform_toolsets.cli`` and never ``file``.

    Repo-shipped templates live under ``profiles/<name>/config.yaml`` if the
    project ships them; otherwise skip -- the runtime contract above is the
    load-bearing assertion.
    """
    template = Path(__file__).resolve().parents[1] / "profiles" / profile / "config.yaml"
    if not template.exists():
        pytest.skip(f"no shipped template for '{profile}' at {template}")
    text = template.read_text(encoding="utf-8")
    import yaml
    parsed = yaml.safe_load(text) or {}
    cli_toolsets = ((parsed.get("platform_toolsets") or {}).get("cli")) or []
    assert "file_readonly" in cli_toolsets, (
        f"{profile} template config.yaml lacks file_readonly on cli platform_toolsets"
    )
    assert "file" not in cli_toolsets, (
        f"{profile} template config.yaml still enables the write-capable 'file' toolset"
    )
