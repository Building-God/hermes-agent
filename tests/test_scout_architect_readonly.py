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
- The LIVE scout/architect profile configs (``<platform home>/profiles/<name>/``)
  also resolve read-only: their ``platform_toolsets.cli`` / ``toolsets`` list
  ``file_readonly`` (never ``file``), and the resolved tool names contain no
  ``write_file`` / ``patch``.

Runs in-process against a temp HERMES_HOME (autouse ``_isolate_hermes_home``
in ``tests/conftest.py``). The live-profile test reads the real scout/architect
``config.yaml`` files from the platform profile store (read-only -- no writes to
state.db / kanban.db, no subprocess) and skips on hosts without those profiles.
No ``HERMES_STATE_DB_GUARD_BYPASS``; no provider or network I/O.
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
def test_live_profile_configs_use_file_readonly(profile: str, monkeypatch) -> None:
    """The LIVE scout/architect profile configs must expose only the read-only
    file toolset, never ``write_file`` / ``patch``.

    Reads the real profile store (``<platform home>/profiles/<name>/config.yaml``)
    directly -- the hermetic fixture redirects HERMES_HOME to a tempdir, so we ask
    ``hermes_constants`` for the platform-native home instead of ``get_hermes_home()``.
    On hosts with no live profile (CI), skip: the runtime contract above and the
    acceptance shell spot-check cover those hosts.
    """
    from hermes_constants import _get_platform_default_hermes_home

    profile_config = (
        Path(_get_platform_default_hermes_home()) / "profiles" / profile / "config.yaml"
    )
    if not profile_config.exists():
        pytest.skip(f"no live '{profile}' profile config at {profile_config}")

    import yaml

    parsed = yaml.safe_load(profile_config.read_text(encoding="utf-8")) or {}

    # platform_toolsets.cli must name file_readonly, never the write-capable file.
    cli = ((parsed.get("platform_toolsets") or {}).get("cli")) or []
    assert "file_readonly" in cli, (
        f"{profile} profile platform_toolsets.cli lacks file_readonly: {cli}"
    )
    assert "file" not in cli, (
        f"{profile} profile platform_toolsets.cli still enables write-capable 'file': {cli}"
    )

    # The top-level toolsets list (when present) must be read-only too.
    top = parsed.get("toolsets") or []
    if top:
        assert "file_readonly" in top, (
            f"{profile} profile toolsets lacks file_readonly: {top}"
        )
        assert "file" not in top, (
            f"{profile} profile toolsets still enables 'file': {top}"
        )

    # Resolve the enabled CLI toolsets to the actual model-visible tool names.
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import resolve_toolset

    enabled = _get_platform_tools(parsed, "cli", include_default_mcp_servers=False)
    assert "file_readonly" in enabled, (
        f"{profile} CLI resolution dropped file_readonly; got {sorted(enabled)}"
    )
    assert "file" not in enabled, (
        f"{profile} CLI resolution re-enabled 'file'; got {sorted(enabled)}"
    )

    resolved_tools: set[str] = set()
    for ts in enabled:
        resolved_tools.update(resolve_toolset(ts))

    assert "read_file" in resolved_tools and "search_files" in resolved_tools, (
        f"{profile} file_readonly failed to contribute read tools; {sorted(resolved_tools)}"
    )
    forbidden = resolved_tools & {"write_file", "patch"}
    assert not forbidden, (
        f"{profile} live session resolves to write-capable file tools "
        f"{sorted(forbidden)}; full set {sorted(resolved_tools)}"
    )

    # Drive the summary renderer against the live config; no write tokens may leak.
    import io
    from contextlib import redirect_stdout

    from hermes_cli import tools_config

    monkeypatch.setattr(tools_config, "_get_enabled_platforms", lambda: ["cli"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        tools_config._print_tools_summary(parsed, ["cli"])
    output = buf.getvalue()

    assert "file (read-only)" in output.lower() or "file_readonly" in output.lower(), (
        f"{profile} tools --summary did not surface the read-only file toolset: {output}"
    )
    match = _FORBIDDEN.search(output)
    assert match is None, (
        f"{profile} tools --summary leaked write-capable token {match.group(0)!r}: {output}"
    )
