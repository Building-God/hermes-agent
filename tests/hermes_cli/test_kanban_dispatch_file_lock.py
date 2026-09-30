"""Per-card file ownership: colliding workers defer, not clobber (lane 0.24).

A card declares the paths it edits as ``holds: <path>`` lines in its body.
When a candidate's declared holds intersect a RUNNING card's declared holds,
``dispatch_once`` must NOT spawn the candidate: it records a defer entry
carrying the literal ``locked by <running task id>`` marker and appends the
same record to the deferred card's event log, so ``kanban show`` / the dash
can surface the collision to the operator. Cards with no ``holds:`` lines are
untouched (guard inert for legacy cards), and once the holder leaves
``running`` the deferred card spawns on a later tick - no permanent deadlock.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _spy_spawn(spawn_calls: list):
    def spy_spawn(task, workspace_path, board=None):
        spawn_calls.append(getattr(task, "id", task))
        return 999999
    return spy_spawn


def _defer_events(task_id: str) -> list[dict]:
    import sqlite3

    db = sqlite3.connect(kb.kanban_db_path(board="default"))
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'deferred_file_locked'",
            (task_id,),
        ).fetchall()
    finally:
        db.close()
    return [json.loads(r["payload"]) for r in rows if r["payload"]]


def test_same_file_second_card_defers_with_locked_by_marker(conn, all_assignees_spawnable):
    """Second card holding the same file is NOT spawned; its defer record and
    task event both carry the literal ``locked by <running task id>``."""
    holder_id = kb.create_task(
        conn, title="holder", assignee="alice",
        body="edits the router\n\nholds: src/router.py\n",
    )
    second_id = kb.create_task(
        conn, title="second", assignee="bob",
        body="also edits the router\n\nholds: src/router.py\n",
    )

    spawn_calls: list = []
    result = kbd.dispatch_once(conn, spawn_fn=_spy_spawn(spawn_calls))

    assert holder_id in spawn_calls, "the first (holding) card must spawn"
    assert second_id not in spawn_calls, "the same-file card must NOT spawn"
    assert [tid for tid, _note in result.deferred_file_locked] == [second_id]
    note = result.deferred_file_locked[0][1]
    assert note == f"locked by {holder_id}", "defer record must carry the literal marker"

    events = _defer_events(second_id)
    assert events, "the deferred card's event log must carry the defer record"
    assert events[0]["locked_by"] == f"locked by {holder_id}"
    assert events[0]["holder"] == holder_id


def test_disjoint_files_both_spawn(conn, all_assignees_spawnable):
    """Control: cards holding disjoint files both spawn - guard inert."""
    kb.create_task(
        conn, title="a", assignee="alice", body="holds: src/a.py\n",
    )
    kb.create_task(
        conn, title="b", assignee="bob", body="holds: src/b.py\n",
    )

    spawn_calls: list = []
    result = kbd.dispatch_once(conn, spawn_fn=_spy_spawn(spawn_calls))

    assert len(spawn_calls) == 2, "disjoint-file cards must both spawn"
    assert result.deferred_file_locked == []


def test_no_holds_lines_guard_inert(conn, all_assignees_spawnable):
    """Legacy cards without ``holds:`` lines behave exactly as today."""
    kb.create_task(conn, title="legacy a", assignee="alice", body="no declarations\n")
    kb.create_task(conn, title="legacy b", assignee="bob", body="none here either\n")

    spawn_calls: list = []
    result = kbd.dispatch_once(conn, spawn_fn=_spy_spawn(spawn_calls))

    assert len(spawn_calls) == 2
    assert result.deferred_file_locked == []


def test_holds_paths_normalize_case_and_separators(conn, all_assignees_spawnable):
    """``holds:`` comparison is normcase/normpath-normalized: mixed case and
    separator styles are one hold, so the collision is still detected."""
    holder_id = kb.create_task(
        conn, title="holder", assignee="alice",
        body="holds: src/router.py\n",
    )
    second_id = kb.create_task(
        conn, title="second", assignee="bob",
        # Opposite-case drive/dir and backslash separator: same file on disk.
        body="holds: SRC\\Router.PY\n",
    )
    # On POSIX normcase is a no-op, so mixed-separator declarations only
    # collide where normcase folds them (Windows). Pin the contract where it
    # applies rather than asserting platform-dependent behaviour.
    if kbd.parse_holds_paths("holds: SRC\\Router.PY\n") != \
            kbd.parse_holds_paths("holds: src/router.py\n"):
        pytest.skip("normcase does not fold case/separators on this host")

    spawn_calls: list = []
    result = kbd.dispatch_once(conn, spawn_fn=_spy_spawn(spawn_calls))

    assert holder_id in spawn_calls
    assert second_id not in spawn_calls
    assert result.deferred_file_locked == [(second_id, f"locked by {holder_id}")]


def test_deferred_card_spawns_after_holder_completes(conn, all_assignees_spawnable):
    """Release semantics: once the holder leaves ``running``, the deferred
    card spawns on the next ``dispatch_once`` tick."""
    holder_id = kb.create_task(
        conn, title="holder", assignee="alice",
        body="holds: src/router.py\n",
    )
    second_id = kb.create_task(
        conn, title="second", assignee="bob",
        body="holds: src/router.py\n",
    )

    spawn_calls: list = []
    kbd.dispatch_once(conn, spawn_fn=_spy_spawn(spawn_calls))
    assert second_id not in spawn_calls

    # Holder completes (the worker's terminal transition). The claim is live
    # (spy spawn returned a pid), so force the operator override.
    kb.complete_task(conn, holder_id, result="done", force=True)

    spawned_tick2: list = []
    result2 = kbd.dispatch_once(conn, spawn_fn=_spy_spawn(spawned_tick2))
    assert second_id in spawned_tick2, "deferred card must spawn after the holder completes"
    assert result2.deferred_file_locked == []


def test_parse_holds_paths_contract():
    """Pinned declaration contract: one or more ``holds: <path>`` lines in the
    card body; values normalized via normpath+normcase; no lines -> []."""
    import os

    body = "File ownership\n\nholds: src/a.py\nholds:  ./src/b.py  \nholds: 'docs/guide.md'\n"
    assert kbd.parse_holds_paths(body) == [
        os.path.normcase(os.path.normpath("src/a.py")),
        os.path.normcase(os.path.normpath("src/b.py")),
        os.path.normcase(os.path.normpath("docs/guide.md")),
    ]
    assert kbd.parse_holds_paths("no declarations here") == []
    assert kbd.parse_holds_paths(None) == []
    # Plain prose mentioning the word must not be parsed as a declaration.
    assert kbd.parse_holds_paths("this card holds: nothing declared") == []
