"""Tests for the PR-guard portion of check_respawn_guard.

Covers:
- open PR must guard
- merged PR must not guard
- closed-unmerged PR must not guard
- older bare URL followed by newer close/merge marker must not guard
- mixed multiple PRs must respect only currently open ones
- GitHub API failure falls back to fail-closed (guard if not text-cleared)
- live GitHub state cache honours TTL
"""

from __future__ import annotations

import sqlite3
import time
import unittest.mock
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_task(conn: sqlite3.Connection, title: str = "pr-task") -> str:
    """Create a ready task and return its id."""
    tid = kb.create_task(conn, title=title, assignee="pilot")
    return tid


def _add_comment(conn: sqlite3.Connection, task_id: str, body: str, at: int) -> None:
    """Insert a comment at a specific epoch timestamp."""
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'worker', ?, ?)",
        (task_id, body, at),
    )
    conn.commit()


def _mock_gh(monkeypatch, responses: dict[str, str]) -> None:
    """Patch _fetch_github_pr_state with a dict of url -> state.

    Also clears the module-level cache so prior test runs don't bleed through.
    """
    kbd._github_pr_state_cache.clear()

    def _fake(url: str) -> str:
        return responses.get(url.rstrip("/").lower(), "")

    monkeypatch.setattr(kbd, "_fetch_github_pr_state", _fake)


# ---------------------------------------------------------------------------
# Core guard cases
# ---------------------------------------------------------------------------


def test_open_pr_guards(kanban_home, monkeypatch):
    """A PR URL in a recent comment for an OPEN PR must guard the task."""
    pr_url = "https://github.com/Building-God/Jarvis/pull/200"
    _mock_gh(monkeypatch, {pr_url.lower(): "OPEN"})

    now = 5_000_000
    monkeypatch.setattr(time, "time", lambda: now)

    with kbc.connect() as conn:
        tid = _make_task(conn)
        _add_comment(conn, tid, f"opened PR: {pr_url}", now - 60)
        result = kbd.check_respawn_guard(conn, tid)

    assert result == "active_pr", f"expected active_pr, got {result!r}"


def test_merged_pr_does_not_guard(kanban_home, monkeypatch):
    """A PR URL for a MERGED PR must NOT guard (even without text markers)."""
    pr_url = "https://github.com/Building-God/Jarvis/pull/110"
    _mock_gh(monkeypatch, {pr_url.lower(): "MERGED"})

    now = 5_000_000
    monkeypatch.setattr(time, "time", lambda: now)

    with kbc.connect() as conn:
        tid = _make_task(conn)
        _add_comment(conn, tid, f"Forward corrective PR opened: **{pr_url}**", now - 60)
        result = kbd.check_respawn_guard(conn, tid)

    assert result is None, f"expected None (merged PR), got {result!r}"


def test_closed_unmerged_pr_does_not_guard(kanban_home, monkeypatch):
    """A PR URL for a CLOSED (not merged) PR must NOT guard."""
    pr_url = "https://github.com/Building-God/Jarvis/pull/105"
    _mock_gh(monkeypatch, {pr_url.lower(): "CLOSED"})

    now = 5_000_000
    monkeypatch.setattr(time, "time", lambda: now)

    with kbc.connect() as conn:
        tid = _make_task(conn)
        _add_comment(conn, tid, f"opened PR: {pr_url}", now - 3600)
        result = kbd.check_respawn_guard(conn, tid)

    assert result is None, f"expected None (closed-unmerged PR), got {result!r}"


def test_older_bare_url_cleared_by_newer_closed_marker(kanban_home, monkeypatch):
    """Older bare URL comment must be cleared when a newer comment documents
    the PR as closed (text-based path, no API call needed)."""
    pr_url = "https://github.com/Building-God/Jarvis/pull/105"
    # API should not be needed here since the newer comment has a text marker.
    _mock_gh(monkeypatch, {})  # empty -> any call would return ""

    now = 5_000_000
    monkeypatch.setattr(time, "time", lambda: now)

    with kbc.connect() as conn:
        tid = _make_task(conn)
        # Older comment: bare URL (no closed marker)
        _add_comment(conn, tid, f"opened PR: {pr_url}", now - 7200)
        # Newer comment: documents the same PR as closed
        _add_comment(conn, tid, f"PR #105 CLOSED - superseded by main push", now - 60)
        result = kbd.check_respawn_guard(conn, tid)

    assert result is None, f"expected None (older URL cleared by newer marker), got {result!r}"


def test_mixed_prs_only_open_one_guards(kanban_home, monkeypatch):
    """With multiple PR URLs, only a currently-open one should cause guarding."""
    url_closed = "https://github.com/Building-God/Jarvis/pull/105"
    url_merged = "https://github.com/Building-God/Jarvis/pull/110"
    url_open = "https://github.com/Building-God/Jarvis/pull/109"

    _mock_gh(monkeypatch, {
        url_closed.lower(): "CLOSED",
        url_merged.lower(): "MERGED",
        url_open.lower(): "OPEN",
    })

    now = 5_000_000
    monkeypatch.setattr(time, "time", lambda: now)

    with kbc.connect() as conn:
        tid = _make_task(conn)
        _add_comment(conn, tid, f"closed: {url_closed}", now - 7200)
        _add_comment(conn, tid, f"merged: {url_merged}", now - 3600)
        _add_comment(conn, tid, f"still open: {url_open}", now - 60)
        result = kbd.check_respawn_guard(conn, tid)

    assert result == "active_pr", f"expected active_pr (open PR present), got {result!r}"


def test_mixed_prs_all_closed_no_guard(kanban_home, monkeypatch):
    """With multiple PR URLs all closed/merged, no guard."""
    url_closed = "https://github.com/Building-God/Jarvis/pull/105"
    url_merged = "https://github.com/Building-God/Jarvis/pull/110"

    _mock_gh(monkeypatch, {
        url_closed.lower(): "CLOSED",
        url_merged.lower(): "MERGED",
    })

    now = 5_000_000
    monkeypatch.setattr(time, "time", lambda: now)

    with kbc.connect() as conn:
        tid = _make_task(conn)
        _add_comment(conn, tid, f"closed: {url_closed}", now - 7200)
        _add_comment(conn, tid, f"merged: {url_merged}", now - 60)
        result = kbd.check_respawn_guard(conn, tid)

    assert result is None, f"expected None (all closed/merged), got {result!r}"


def test_github_api_failure_falls_back_to_guard(kanban_home, monkeypatch):
    """When GitHub API fails (empty string return), fall back to guarding
    if the comment has no text-based closed marker."""
    pr_url = "https://github.com/Building-God/Jarvis/pull/200"
    _mock_gh(monkeypatch, {pr_url.lower(): ""})  # API failure

    now = 5_000_000
    monkeypatch.setattr(time, "time", lambda: now)

    with kbc.connect() as conn:
        tid = _make_task(conn)
        _add_comment(conn, tid, f"opened PR: {pr_url}", now - 60)
        result = kbd.check_respawn_guard(conn, tid)

    assert result == "active_pr", (
        f"expected active_pr (API fail, no text marker), got {result!r}"
    )


def test_github_api_failure_text_closed_marker_clears(kanban_home, monkeypatch):
    """When GitHub API fails but the comment itself has a closed text marker,
    the text evidence is authoritative and the guard is lifted."""
    pr_url = "https://github.com/Building-God/Jarvis/pull/105"
    _mock_gh(monkeypatch, {pr_url.lower(): ""})  # API failure

    now = 5_000_000
    monkeypatch.setattr(time, "time", lambda: now)

    with kbc.connect() as conn:
        tid = _make_task(conn)
        # Comment with the URL AND a text closed marker in the same comment
        _add_comment(conn, tid, f"PR #105 CLOSED - see {pr_url}", now - 60)
        result = kbd.check_respawn_guard(conn, tid)

    assert result is None, (
        f"expected None (text closed marker present), got {result!r}"
    )


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_github_state_cache_prevents_repeated_calls(monkeypatch):
    """_fetch_github_pr_state hits the cache on second call within TTL."""
    kbd._github_pr_state_cache.clear()

    call_count = 0

    def _fake_subprocess_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        mock_result = unittest.mock.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "MERGED\n"
        return mock_result

    monkeypatch.setattr(kbd.subprocess, "run", _fake_subprocess_run)

    url = "https://github.com/example/repo/pull/1"
    state1 = kbd._fetch_github_pr_state(url)
    state2 = kbd._fetch_github_pr_state(url)

    assert state1 == "MERGED"
    assert state2 == "MERGED"
    assert call_count == 1, f"expected 1 subprocess call (cache hit), got {call_count}"


def test_github_state_cache_expires_after_ttl(monkeypatch):
    """After the TTL, _fetch_github_pr_state makes a fresh call."""
    kbd._github_pr_state_cache.clear()

    call_count = 0
    now_val = [5_000_000.0]

    def _fake_time():
        return now_val[0]

    def _fake_subprocess_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        mock_result = unittest.mock.MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "MERGED\n"
        return mock_result

    monkeypatch.setattr(time, "time", _fake_time)
    monkeypatch.setattr(kbd.subprocess, "run", _fake_subprocess_run)
    # Also patch time.time inside kbd module
    monkeypatch.setattr(kbd.time, "time", _fake_time)

    url = "https://github.com/example/repo/pull/2"
    kbd._fetch_github_pr_state(url)  # call 1

    # Advance past TTL
    now_val[0] += kbd._GITHUB_PR_STATE_CACHE_TTL + 1
    kbd._fetch_github_pr_state(url)  # call 2 (cache expired)

    assert call_count == 2, f"expected 2 subprocess calls (TTL expired), got {call_count}"


# ---------------------------------------------------------------------------
# Text-marker edge cases
# ---------------------------------------------------------------------------


def test_text_marker_pr_number_equals_closed_not_matched(kanban_home, monkeypatch):
    """'PR #105 = CLOSED UNMERGED' with '=' separator - the text regex does NOT
    match this (it requires space before CLOSED), so the API check is used.
    API returning CLOSED should still clear the guard."""
    pr_url = "https://github.com/Building-God/Jarvis/pull/105"
    _mock_gh(monkeypatch, {pr_url.lower(): "CLOSED"})

    now = 5_000_000
    monkeypatch.setattr(time, "time", lambda: now)

    with kbc.connect() as conn:
        tid = _make_task(conn)
        body = (
            f"PR #105 = CLOSED UNMERGED (mergedAt null). "
            f"See: {pr_url}"
        )
        _add_comment(conn, tid, body, now - 60)
        result = kbd.check_respawn_guard(conn, tid)

    assert result is None, (
        f"expected None (API confirmed CLOSED), got {result!r}"
    )
