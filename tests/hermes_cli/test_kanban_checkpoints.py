"""Behavior contracts for durable, owned Kanban task checkpoints."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def checkpoint_conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = tmp_path / "kanban-checkpoints.db"
    with kbc.connect(db_path) as conn:
        yield conn


def _claim(conn, title="checkpoint task"):
    task_id = kb.create_task(conn, title=title, assignee="worker")
    task = kb.claim_task(conn, task_id, claimer="checkpoint-test")
    assert task is not None and task.current_run_id is not None
    return task_id, task.current_run_id


def test_checkpoint_round_trip_is_canonical_and_task_scoped(checkpoint_conn):
    task_id, run_id = _claim(checkpoint_conn)
    other_task_id, _ = _claim(checkpoint_conn, "other task")

    saved = kb.save_task_checkpoint(
        checkpoint_conn, task_id, expected_run_id=run_id,
        progress={"z": [1, 2], "a": "progress"},
    )

    loaded = kb.load_task_checkpoint(checkpoint_conn, task_id)
    assert loaded == saved
    assert loaded.progress == {"a": "progress", "z": [1, 2]}
    assert kb.load_task_checkpoint(checkpoint_conn, other_task_id) is None
    stored = checkpoint_conn.execute(
        "SELECT progress_json, payload_sha256 FROM task_checkpoints WHERE task_id = ?", (task_id,),
    ).fetchone()
    assert stored["progress_json"] == '{"a":"progress","z":[1,2]}'
    assert stored["payload_sha256"] == hashlib.sha256(
        stored["progress_json"].encode("utf-8"),
    ).hexdigest()


def test_checkpoint_idempotency_is_stable_but_rejects_conflicting_progress(checkpoint_conn):
    task_id, run_id = _claim(checkpoint_conn)

    first = kb.save_task_checkpoint(
        checkpoint_conn, task_id, expected_run_id=run_id, progress={"step": 1}, idempotency_key="retry-1",
    )
    retry = kb.save_task_checkpoint(
        checkpoint_conn, task_id, expected_run_id=run_id, progress={"step": 1}, idempotency_key="retry-1",
    )

    assert retry == first
    assert checkpoint_conn.execute(
        "SELECT COUNT(*) FROM task_checkpoints WHERE task_id = ?", (task_id,),
    ).fetchone()[0] == 1
    with pytest.raises(kb.CheckpointIdempotencyError):
        kb.save_task_checkpoint(
            checkpoint_conn, task_id, expected_run_id=run_id, progress={"step": 2}, idempotency_key="retry-1",
        )


def test_checkpoint_rejects_stale_run_and_sequences_continue_across_attempts(checkpoint_conn):
    task_id, first_run_id = _claim(checkpoint_conn)
    first = kb.save_task_checkpoint(
        checkpoint_conn, task_id, expected_run_id=first_run_id, progress={"attempt": 1},
    )
    checkpoint_conn.execute(
        "UPDATE task_runs SET status = 'reclaimed', outcome = 'reclaimed', ended_at = 1 WHERE id = ?",
        (first_run_id,),
    )
    checkpoint_conn.execute(
        "UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, current_run_id = NULL "
        "WHERE id = ?",
        (task_id,),
    )
    second = kb.claim_task(checkpoint_conn, task_id, claimer="checkpoint-test-reclaimed")
    assert second is not None and second.current_run_id is not None

    with pytest.raises(kb.CheckpointOwnershipError):
        kb.save_task_checkpoint(
            checkpoint_conn, task_id, expected_run_id=first_run_id, progress={"attempt": "stale"},
        )
    latest = kb.save_task_checkpoint(
        checkpoint_conn, task_id, expected_run_id=second.current_run_id, progress={"attempt": 2},
    )

    assert first.sequence == 1
    assert latest.sequence == 2
    assert kb.load_task_checkpoint(checkpoint_conn, task_id) == latest


@pytest.mark.parametrize(
    ("column", "value"),
    [("payload_sha256", "tampered"), ("version", 99)],
)
def test_checkpoint_load_fails_closed_on_tampered_digest_or_version(checkpoint_conn, column, value):
    task_id, run_id = _claim(checkpoint_conn)
    kb.save_task_checkpoint(
        checkpoint_conn, task_id, expected_run_id=run_id, progress={"safe": True},
    )
    checkpoint_conn.execute(
        f"UPDATE task_checkpoints SET {column} = ? WHERE task_id = ?", (value, task_id),
    )

    with pytest.raises(kb.CheckpointCorruptionError):
        kb.load_task_checkpoint(checkpoint_conn, task_id)


def test_checkpoint_rejects_non_object_and_oversized_progress(checkpoint_conn):
    task_id, run_id = _claim(checkpoint_conn)

    with pytest.raises(ValueError, match="JSON object"):
        kb.save_task_checkpoint(checkpoint_conn, task_id, expected_run_id=run_id, progress=["not", "an", "object"])
    with pytest.raises(ValueError, match="exceeds"):
        kb.save_task_checkpoint(
            checkpoint_conn, task_id, expected_run_id=run_id, progress={"large": "x" * (64 * 1024)},
        )
