"""Queue-quiet fixes (t_60ab3587): catch bad assignees, recover stranded
cards, force "superseded" to mean done, and signal a zero-dispatchable
backlog.

Thin unit slices over ``kanban_db`` / ``kanban_db_dispatch`` using an
in-memory board. The live acceptance is the gateway-side recovery pass and the
zero-dispatchable watcher alert (deployed by the reviewer).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT",
                "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    # Default to "no claim allowlist" so create_task accepts any assignee;
    # the assignee-validation tests override this with a real allowlist.
    monkeypatch.setattr(kbd, "_dispatch_profile_allowlist", lambda normalize: None)
    return home


def _conn():
    return kbc.connect()


def _allowlist(names):
    def _fn(normalize):
        return frozenset(names)
    return _fn


# --- Slice 3: "Closing this card as superseded" actually closes the card ---

def test_superseded_comment_closes_card(fresh_home):
    conn = _conn()
    try:
        tid = kb.create_task(conn, title="old work", assignee="pilot")
        assert kb.get_task(conn, tid).status == "ready"

        kb.add_comment(conn, tid, author="pilot", body="Closing this card as superseded")

        assert kb.get_task(conn, tid).status == "done"
        assert kb.get_task(conn, tid).result == "superseded"
    finally:
        conn.close()


def test_superseded_comment_ignored_when_author_not_assignee(fresh_home):
    conn = _conn()
    try:
        tid = kb.create_task(conn, title="old work", assignee="pilot")
        kb.add_comment(conn, tid, author="reviewer", body="Closing this card as superseded")
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


def test_plain_comment_does_not_close(fresh_home):
    conn = _conn()
    try:
        tid = kb.create_task(conn, title="old work", assignee="pilot")
        kb.add_comment(conn, tid, author="pilot", body="this is superseded by the new approach")
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


# --- Slice 1: bogus assignee is rejected at creation ---

def test_create_rejects_unknown_assignee(fresh_home, monkeypatch):
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in {"pilot", "reviewer"})
    monkeypatch.setattr(kbd, "_dispatch_profile_allowlist", _allowlist({"pilot", "reviewer"}))

    conn = _conn()
    try:
        with pytest.raises(ValueError):
            kb.create_task(conn, title="stranded", assignee="staging-draft")
        # A real profile still passes.
        tid = kb.create_task(conn, title="ok", assignee="pilot")
        assert kb.get_task(conn, tid).assignee == "pilot"
    finally:
        conn.close()


def test_create_allows_unknown_assignee_when_no_allowlist(fresh_home, monkeypatch):
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    monkeypatch.setattr(kbd, "_dispatch_profile_allowlist", lambda normalize: None)

    conn = _conn()
    try:
        # Fail-open: no claim allowlist declared, so a name can't be judged.
        tid = kb.create_task(conn, title="stranded", assignee="staging-draft")
        assert kb.get_task(conn, tid).assignee == "staging-draft"
    finally:
        conn.close()


def test_create_accepts_real_profile_outside_allowlist(fresh_home, monkeypatch):
    import hermes_cli.profiles as profiles
    # maestro is a real profile but not in this home's allowlist - still accepted.
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "maestro")
    monkeypatch.setattr(kbd, "_dispatch_profile_allowlist", _allowlist({"pilot", "reviewer"}))

    conn = _conn()
    try:
        tid = kb.create_task(conn, title="maestro card", assignee="maestro")
        assert kb.get_task(conn, tid).assignee == "maestro"
    finally:
        conn.close()


# --- Slice 2: stranded recovery helpers ---

def test_assignee_is_real_profile_ungated(fresh_home, monkeypatch):
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in {"pilot", "sage"})

    assert kbd._assignee_is_real_profile("pilot") is True
    assert kbd._assignee_is_real_profile("sage") is True   # real even if foreign-home
    assert kbd._assignee_is_real_profile("staging-draft") is False


def test_apply_default_assignee_reassigns_stranded(fresh_home):
    conn = _conn()
    try:
        tid = kb.create_task(conn, title="stranded", assignee="staging-draft")
        assert kb.get_task(conn, tid).assignee == "staging-draft"

        ok = kbd._apply_default_assignee(
            conn, tid, "pilot", dry_run=False, from_assignee="staging-draft",
        )
        assert ok is True
        assert kb.get_task(conn, tid).assignee == "pilot"
        events = kb.list_events(conn, tid)
        assigned = [e for e in events if e.kind == "assigned"]
        assert any((e.payload or {}).get("source") == "kanban.recover_stranded" for e in assigned)
    finally:
        conn.close()


# --- Slice 4: zero-dispatchable backlog signal ---

def test_has_ready_backlog_detects_non_spawnable_ready(fresh_home):
    conn = _conn()
    try:
        assert kbd.has_ready_backlog(conn) is False

        kb.create_task(conn, title="stranded", assignee="staging-draft")
        # A ready row exists regardless of whether its assignee is a real profile.
        assert kbd.has_ready_backlog(conn) is True
    finally:
        conn.close()
