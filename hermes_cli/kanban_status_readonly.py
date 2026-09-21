"""Bounded, read-only operator view of Kanban activity and result evidence.

This is deliberately separate from ``kanban_show``: a status question must not
dump card bodies, comment text, run summaries, or notification routing.
"""

from __future__ import annotations

import sqlite3
import json
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


OPEN_STATUSES = frozenset({"triage", "todo", "scheduled", "ready", "running", "blocked", "review"})
_SETTLED_EVENT_KINDS = {"done": ("completed",), "blocked": ("blocked", "gave_up", "timed_out")}


def _utc(value: object) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), UTC).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def read_only_connection(board: str | None = None) -> sqlite3.Connection:
    """Open an existing board without creating/migrating it or taking a writer."""
    from hermes_cli import kanban_db as kb

    path = Path(kb.kanban_db_path(board=board)).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def _result_evidence(conn: sqlite3.Connection, task: sqlite3.Row) -> dict:
    run = _one(
        conn,
        "SELECT summary, outcome FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task["id"],),
    )
    attachments = _one(conn, "SELECT COUNT(*) AS n FROM task_attachments WHERE task_id=?", (task["id"],))
    sources = []
    if task["result"]:
        sources.append("task_result")
    if run and run["summary"]:
        sources.append("run_summary")
    if attachments and attachments["n"]:
        sources.append("attachments")
    return {
        "available": bool(sources),
        "sources": sources,
        "attachment_count": int(attachments["n"] if attachments else 0),
        "last_run_outcome": run["outcome"] if run else None,
    }


def _notification_cursor(conn: sqlite3.Connection, task: sqlite3.Row) -> str:
    """Report notifier bookkeeping, never provider delivery or human receipt."""
    kinds = _SETTLED_EVENT_KINDS.get(task["status"])
    if not kinds:
        return "not_applicable"
    marks = ",".join("?" for _ in kinds)
    event = _one(
        conn,
        f"SELECT id FROM task_events WHERE task_id=? AND kind IN ({marks}) ORDER BY id DESC LIMIT 1",
        (task["id"], *kinds),
    )
    if event is None:
        return "unknown"
    try:
        subs = conn.execute(
            "SELECT last_event_id, delivery_failures, claim_token "
            "FROM kanban_notify_subs WHERE task_id=?", (task["id"],),
        ).fetchall()
    except sqlite3.OperationalError:
        return "unknown"
    if not subs:
        return "no_subscriber"
    if any(
        sub["last_event_id"] is None
        or int(sub["last_event_id"]) < int(event["id"])
        or int(sub["delivery_failures"] or 0) > 0
        or sub["claim_token"] is not None
        for sub in subs
    ):
        return "pending_or_retrying"
    return "cursor_settled_not_receipt"


def _block_origin(conn: sqlite3.Connection, task_id: str) -> str:
    """Only an explicit typed needs_input block claims a human action."""
    event = _one(
        conn,
        "SELECT kind, payload FROM task_events WHERE task_id=? "
        "AND kind IN ('blocked', 'gave_up') ORDER BY id DESC LIMIT 1",
        (task_id,),
    )
    if event is None:
        return "unknown"
    if event["kind"] == "gave_up":
        return "automatic_failure"
    try:
        payload = json.loads(event["payload"] or "{}")
    except (TypeError, ValueError):
        return "unspecified"
    if not isinstance(payload, dict):
        return "unspecified"
    return payload.get("kind") if payload.get("kind") in {"needs_input", "capability", "transient"} else "unspecified"


def _checkpoint_note_at(conn: sqlite3.Connection, task_id: str) -> str | None:
    """Return the timestamp of a human-authored CHECKPOINT note, never its text."""
    note = _one(
        conn,
        "SELECT MAX(created_at) AS at FROM task_comments "
        "WHERE task_id=? AND ltrim(body) LIKE 'CHECKPOINT:%'",
        (task_id,),
    )
    return _utc(note["at"]) if note else None


def _typed_checkpoint(conn: sqlite3.Connection, task_id: str) -> dict:
    """Report checkpoint metadata without reading progress or validating it."""
    table = _one(
        conn,
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_checkpoints'",
    )
    if table is None:
        return {"status": "unavailable"}
    checkpoint = _one(
        conn,
        "SELECT sequence, run_id, payload_sha256, created_at FROM task_checkpoints "
        "WHERE task_id=? ORDER BY sequence DESC LIMIT 1",
        (task_id,),
    )
    if checkpoint is None:
        return {"status": "absent"}
    return {
        "status": "present",
        "sequence": int(checkpoint["sequence"]),
        "source_run_id": int(checkpoint["run_id"]),
        "created_at": _utc(checkpoint["created_at"]),
        "payload_sha256": checkpoint["payload_sha256"],
    }


def _worker_log_activity(task_id: str, board: str, active_started_at: int | None) -> dict:
    """Report only writes in this attempt; log bytes may span earlier attempts."""
    from hermes_cli import kanban_db as kb

    if active_started_at is None:
        return {"last_write_at": None, "size_bytes": None}
    try:
        stat = kb.worker_log_path(task_id, board=board).stat()
    except OSError:
        return {"last_write_at": None, "size_bytes": None}
    if stat.st_mtime < active_started_at:
        return {"last_write_at": None, "size_bytes": None}
    return {"last_write_at": _utc(stat.st_mtime), "size_bytes": stat.st_size}


def _open_prerequisites(conn: sqlite3.Connection, task_id: str) -> dict:
    """Bounded dependency state, without reading parent bodies or comments."""
    rows = conn.execute(
        "SELECT p.id, p.status FROM task_links l JOIN tasks p ON p.id=l.parent_id "
        "WHERE l.child_id=? AND p.status NOT IN ('done','archived') "
        "ORDER BY p.id LIMIT 6", (task_id,),
    ).fetchall()
    prerequisites = []
    for parent in rows[:5]:
        item = {"id": parent["id"], "status": parent["status"]}
        if parent["status"] == "blocked":
            item["block_origin"] = _block_origin(conn, parent["id"])
            item["human_action"] = (
                "explicitly_requested" if item["block_origin"] == "needs_input" else "not_recorded"
            )
        prerequisites.append(item)
    return {"tasks": prerequisites, "truncated": len(rows) > 5}


def _task_row(conn: sqlite3.Connection, task: sqlite3.Row, *, completed: bool, board: str) -> dict:
    row = {
        "id": task["id"],
        "title": task["title"],
        "status": task["status"],
        "assignee": task["assignee"],
        "result_evidence": _result_evidence(conn, task),
        "notification_cursor": _notification_cursor(conn, task),
        "human_receipt": "unproven",
    }
    if completed:
        row["completed_at"] = _utc(task["completed_at"])
    else:
        event = _one(
            conn, "SELECT kind, created_at FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task["id"],),
        )
        row["last_event"] = {
            "kind": event["kind"] if event else None,
            "at": _utc(event["created_at"]) if event else None,
        }
        row["last_checkpoint_note_at"] = _checkpoint_note_at(conn, task["id"])
        # Kept for existing JSON consumers; it is a CHECKPOINT comment timestamp,
        # not evidence that a durable checkpoint exists or is loadable.
        row["last_checkpoint_at"] = row["last_checkpoint_note_at"]
        row["typed_checkpoint"] = _typed_checkpoint(conn, task["id"])
        if task["status"] == "blocked":
            row["block_origin"] = _block_origin(conn, task["id"])
            row["human_action"] = (
                "explicitly_requested" if row["block_origin"] == "needs_input" else "not_recorded"
            )
        if task["status"] == "todo":
            row["open_prerequisites"] = _open_prerequisites(conn, task["id"])
        if task["status"] == "running":
            row["worker_liveness"] = {
                "last_heartbeat_at": _utc(task["active_last_heartbeat_at"]),
                "log": _worker_log_activity(task["id"], board, task["active_started_at"]),
                "runtime_cap_at": _utc(
                    task["active_started_at"] + task["max_runtime_seconds"]
                    if task["active_started_at"] is not None and task["max_runtime_seconds"] is not None
                    else None
                ),
            }
    return row


def build_snapshot(
    conn: sqlite3.Connection, *, board: str, open_limit: int = 50, completed_limit: int = 10,
) -> dict:
    if not 1 <= open_limit <= 100:
        raise ValueError("open_limit must be between 1 and 100")
    if not 1 <= completed_limit <= 20:
        raise ValueError("completed_limit must be between 1 and 20")
    counts = Counter({row["status"]: row["n"] for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks WHERE status != 'archived' GROUP BY status"
    ).fetchall()})
    marks = ",".join("?" for _ in OPEN_STATUSES)
    open_rows = conn.execute(
        "SELECT t.id, t.title, t.status, t.assignee, t.completed_at, t.result, t.created_at, "
        "t.max_runtime_seconds, r.started_at AS active_started_at, "
        "r.last_heartbeat_at AS active_last_heartbeat_at "
        "FROM tasks t LEFT JOIN task_runs r ON r.id=t.current_run_id AND r.task_id=t.id "
        f"WHERE t.status IN ({marks}) "
        "ORDER BY CASE t.status WHEN 'running' THEN 0 WHEN 'blocked' THEN 1 "
        "WHEN 'review' THEN 2 WHEN 'ready' THEN 3 ELSE 4 END, "
        "t.created_at ASC, t.id ASC LIMIT ?",
        (*sorted(OPEN_STATUSES), open_limit),
    ).fetchall()
    done_rows = conn.execute(
        "SELECT t.id, t.title, t.status, t.assignee, t.completed_at, t.result, t.created_at, "
        "t.max_runtime_seconds, r.started_at AS active_started_at, "
        "r.last_heartbeat_at AS active_last_heartbeat_at "
        "FROM tasks t LEFT JOIN task_runs r ON r.id=t.current_run_id AND r.task_id=t.id "
        "WHERE t.status='done' "
        "ORDER BY t.completed_at DESC, t.id DESC LIMIT ?", (completed_limit,),
    ).fetchall()
    open_total = sum(counts[status] for status in OPEN_STATUSES)
    return {
        "board": board,
        "summary": {
            "open": open_total, "done": counts["done"], "blocked": counts["blocked"],
            "running": counts["running"], "ready": counts["ready"],
            # Blocked records stay open, but are not a runnable queue or live work.
            "idle": counts["running"] == 0 and counts["ready"] == 0,
            "open_shown": len(open_rows), "open_truncated": open_total > len(open_rows),
        },
        "open_tasks": [_task_row(conn, task, completed=False, board=board) for task in open_rows],
        "recent_results": [_task_row(conn, task, completed=True, board=board) for task in done_rows],
        "delivery_caveat": "Notifier cursor state is not provider delivery or human receipt.",
    }


def snapshot_for_board(
    board: str | None = None, *, open_limit: int = 50, completed_limit: int = 10,
) -> dict:
    from hermes_cli import kanban_db as kb

    resolved_board = board or kb.get_current_board()
    with closing(read_only_connection(board=board)) as conn:
        return build_snapshot(
            conn, board=resolved_board, open_limit=open_limit, completed_limit=completed_limit,
        )


def _short(value: object, *, limit: int = 96) -> str:
    """Keep a DB title on one bounded chat line without interpreting markup."""
    clean = " ".join(str(value or "").split())
    return clean[: limit - 1] + "..." if len(clean) > limit else clean


def render_status(snapshot: dict) -> str:
    """Operator-facing summary; never include card bodies, run prose, or log text."""
    summary = snapshot["summary"]
    lines = [
        f"Board {snapshot['board']}: {summary['open']} open "
        f"({summary.get('running', 0)} running, {summary['blocked']} blocked), "
        f"{summary['done']} done.",
        f"Showing {summary['open_shown']} of {summary['open']} open tasks.",
    ]
    if not snapshot["open_tasks"]:
        lines.append("No open tasks.")
    for task in snapshot["open_tasks"]:
        event = task.get("last_event") or {}
        activity = event.get("kind") or "none"
        at = event.get("at") or "unknown time"
        note = task.get("last_checkpoint_note_at", task.get("last_checkpoint_at")) or "none"
        typed = task.get("typed_checkpoint") or {"status": "unavailable"}
        typed_status = typed.get("status", "unavailable")
        if typed_status == "present":
            typed_text = (
                f"sequence {typed.get('sequence')} from run {typed.get('source_run_id')} "
                f"at {typed.get('created_at') or 'unknown time'}"
            )
        else:
            typed_text = typed_status
        lines.append(
            f"- {task['id']} [{task['status']}] {_short(task['title'])} "
            f"(@{_short(task.get('assignee') or 'unassigned', limit=32)}); "
            f"last event {activity} at {at}; CHECKPOINT note {note}; "
            f"typed durable checkpoint {typed_text}"
        )
        if task["status"] == "blocked":
            origin = task.get("block_origin", "unknown")
            action = task.get("human_action", "not_recorded")
            lines.append(f"  Block origin: {origin}; human action: {action}.")
        if task["status"] == "todo":
            wait = task.get("open_prerequisites") or {}
            parents = wait.get("tasks") or []
            if parents:
                labels = [
                    f"{parent['id']} [{parent['status']}"
                    + (f", {parent['block_origin']}, human action: {parent['human_action']}"
                       if parent['status'] == 'blocked' else "")
                    + "]"
                    for parent in parents
                ]
                suffix = ", more not shown" if wait.get("truncated") else ""
                lines.append(f"  Open prerequisites: {', '.join(labels)}{suffix}.")
        if task["status"] == "running":
            live = task.get("worker_liveness") or {}
            log = live.get("log") or {}
            log_size = log.get("size_bytes")
            log_size_text = f" ({log_size} total bytes across attempts)" if log_size is not None else ""
            lines.append(
                f"  Heartbeat: {live.get('last_heartbeat_at') or 'none'} (liveness only); "
                f"worker log write in attempt: {log.get('last_write_at') or 'none'}"
                f"{log_size_text}; "
                f"runtime cap threshold: {live.get('runtime_cap_at') or 'none'}"
            )
    lines.append("Recent results:")
    if not snapshot["recent_results"]:
        lines.append("- none")
    for task in snapshot["recent_results"]:
        evidence = task.get("result_evidence") or {}
        sources = "+".join(evidence.get("sources") or []) or "none"
        cursor = task.get("notification_cursor") or "unknown"
        lines.append(
            f"- {task['id']} {_short(task.get('title'))}: {sources} "
            f"({evidence.get('attachment_count', 0)} attachments); notifier {cursor}"
        )
    lines.append("Notifier state is not proof of message delivery or human receipt.")
    return "\n".join(lines)
