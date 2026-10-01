"""Closed-loop reconciliation within the existing board/dispatcher.

No additional scheduler or runnable store. All deadlines, repairs and exceptions
are durable events on the original task. Human choices are never inferred from
worker faults. Repairs are finite, independently reviewed existing-board tasks.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import time


def policy(conn=None) -> dict:
    """Lifecycle policy belongs to the bound board, independent of worker profile.

    Profiles keep their own credentials/config. Only the current board's existing
    metadata supplies shared lifecycle rules; there is no cross-board inheritance.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.config import load_config
    db = (next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
          if conn is not None else str(kb.kanban_db_path()))
    if db:
        path = Path(db).parent / "board.json"
        if path.exists():
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
                if "operator" in metadata:
                    value = metadata["operator"]
                    return value if isinstance(value, dict) else {"enabled": False}
            except (OSError, ValueError, TypeError):
                return {"enabled": False, "policy_error": "invalid_board_metadata"}
    value = (load_config() or {}).get("kanban", {}).get("operator", {})
    return value if isinstance(value, dict) else {}


def dispatch_owner(conn, preferred=None):
    from hermes_cli.config import load_config
    from hermes_cli.kanban_db_dispatch import _profile_exists_fn
    cfg = policy(conn)
    kanban = (load_config() or {}).get("kanban", {})
    candidate = preferred or cfg.get("continuation_assignee") or kanban.get("default_assignee")
    exists = _profile_exists_fn()
    if not candidate or exists is None or not exists(candidate):
        raise ValueError("no verified dispatchable owner for this board")
    return candidate


def _exception(conn, tid, reason, **details):
    from hermes_cli import kanban_db as kb
    existing = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='operator_exception'", (tid,))
    if any(_payload(row).get("reason") == reason for row in existing):
        return
    with kb.write_txn(conn):
        kb._append_event(conn,tid,"operator_exception",dict(owner="agent",reason=reason,**details))


def _last(conn, task_id, kind):
    return conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                        (task_id, kind)).fetchone()


def _payload(event):
    try:
        return json.loads(event["payload"] or "{}") if event else {}
    except (ValueError, TypeError):
        return {}


def candidate_fingerprint(conn, task_id, metadata):
    """Candidate bytes/checkpoint/receipt data, never cosmetic summary prose."""
    if not isinstance(metadata, dict):
        metadata = {}
    evidence = {}
    artifacts = []
    for value in metadata.get("artifacts", []):
        path = Path(value) if isinstance(value, str) else None
        if path and path.is_file():
            with path.open("rb") as handle:
                artifacts.append(hashlib.file_digest(handle,"sha256").hexdigest())
    if artifacts:
        evidence["artifact_hashes"] = sorted(artifacts)
    if metadata.get("acceptance_receipts"):
        evidence["acceptance_receipts"] = metadata["acceptance_receipts"]
    checkpoint = conn.execute("SELECT payload_sha256 FROM task_checkpoints WHERE task_id=? ORDER BY sequence DESC LIMIT 1", (task_id,)).fetchone()
    if checkpoint:
        evidence["checkpoint"] = checkpoint["payload_sha256"]
    return hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest() if evidence else None


def _reconcile(conn, *, board=None, settings=None, now=None) -> list[dict]:
    """Run before idle selection, even when every dispatch lane is busy.

    The fixed overall deadline never renews on heartbeat/retry. One repair per
    exhausted task is permitted; exhausted repair is an agent-owned exception,
    never a needs_input question. Explicit human/dependency/park holds are kept.
    """
    from hermes_cli import kanban_db as kb
    cfg = settings if settings is not None else policy(conn)
    if not cfg.get("enabled", False):
        return []
    now = int(time.time() if now is None else now)
    runtime = max(60, int(cfg.get("attempt_seconds", 900)))
    deadline = max(runtime, int(cfg.get("request_seconds", 7200)))
    actions = []
    for row in conn.execute("SELECT id FROM tasks WHERE status='blocked' AND block_kind='dependency' AND claim_lock IS NULL").fetchall():
        tid = row["id"]
        if _last(conn, tid, "operator_repair_created"):
            continue  # An unreviewed repair's done flag must not release its parent.
        parents = kb.parent_ids(conn, tid)
        if parents and all(kb.get_task(conn, parent).status == "done" for parent in parents):
            if kb.unblock_task(conn, tid):
                actions.append({"task_id": tid, "dependency_reconciled": parents})

    # A missing attempt ceiling used to make heartbeating workers immortal.
    # Apply a recorded deterministic budget; the existing process-tree enforcer
    # remains the only stop/reclaim implementation.
    with kb.write_txn(conn):
        for row in conn.execute("SELECT t.id,t.current_run_id,r.started_at AS run_started_at FROM tasks t "
                                "LEFT JOIN task_runs r ON r.id=t.current_run_id "
                                "WHERE t.status='running' AND t.max_runtime_seconds IS NULL").fetchall():
            if cfg.get("activation_at") and (row["run_started_at"] or 0) < int(cfg["activation_at"]):
                continue  # Existing attempts did not receive the new deadline contract.
            conn.execute("UPDATE tasks SET max_runtime_seconds=? WHERE id=?", (runtime, row["id"]))
            conn.execute("UPDATE task_runs SET max_runtime_seconds=? WHERE id=? AND max_runtime_seconds IS NULL",
                         (runtime, row["current_run_id"]))
            kb._append_event(conn, row["id"], "operator_attempt_budget", {"seconds": runtime}, run_id=row["current_run_id"])

    rows = conn.execute("SELECT t.* FROM tasks t JOIN task_user_origins o ON o.task_id=t.id "
                        "WHERE t.status NOT IN ('archived')").fetchall()
    for row in rows:
        tid = row["id"]
        armed = _last(conn, tid, "operator_deadline")
        if not armed:
            due = int(row["created_at"]) + deadline
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "operator_deadline", {"due_at": due, "seconds": deadline})
        else:
            due = int(_payload(armed)["due_at"])
        if now >= due and row["status"] != "done" and not _last(conn, tid, "operator_exception"):
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "operator_exception",
                                 {"owner": "agent", "reason": "request_deadline_exceeded", "due_at": due,
                                  "status": row["status"], "assignee": row["assignee"],
                                  "human_choice_pending": row["block_kind"] == "needs_input"})
            actions.append({"task_id": tid, "exception": "request_deadline_exceeded"})

    # Rework deadlines are actionable transitions, not timestamps in a journal.
    for row in conn.execute("SELECT t.* FROM tasks t WHERE t.status IN ('ready','running')").fetchall():
        event = _last(conn,row["id"],"operator_rework_due")
        if not event or now < _payload(event).get("due_at",now+1):
            continue
        later_review = _last(conn,row["id"],"review_requested")
        if later_review and later_review["id"] > event["id"]:
            continue
        _exception(conn,row["id"],"rework_deadline_exceeded",due_at=_payload(event)["due_at"])
        if row["claim_lock"] is None:
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='blocked',block_kind=NULL WHERE id=? AND claim_lock IS NULL",(row["id"],))
                if not _last(conn,row["id"],"gave_up") or _last(conn,row["id"],"gave_up")["id"] < event["id"]:
                    kb._append_event(conn,row["id"],"gave_up",{"error":"rework deadline exceeded", "operator_rework":True,"sticky":True})
        else:
            # Active workers retain the identity fence; the native tree enforcer stops them.
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET max_runtime_seconds=MIN(COALESCE(max_runtime_seconds,?),?) WHERE id=?",(runtime,runtime,row["id"]))

    # Exhausted automatic attempts need a repair owner, not endless blind retries
    # or an invented Harry choice. Respect sticky auth/explicit stop fences.
    pending = conn.execute("SELECT COUNT(*) FROM tasks WHERE created_by='operator-repair' AND status NOT IN ('done','archived','blocked')").fetchone()[0]
    for row in conn.execute("SELECT * FROM tasks WHERE status='blocked' AND block_kind IS NULL "
                            "AND claim_lock IS NULL ORDER BY created_at DESC").fetchall():
        if pending >= int(cfg.get("max_pending_repairs", 1)):
            break
        tid = row["id"]
        fault = _last(conn, tid, "gave_up")
        if not fault or (_payload(fault).get("sticky") and not _payload(fault).get("operator_rework")):
            continue
        if now - fault["created_at"] > int(cfg.get("repair_window_seconds", 172800)):
            continue
        if _last(conn, tid, "operator_repair_created") or row["created_by"] == "operator-repair":
            continue
        reason = _payload(fault).get("error", "automatic worker failure")
        body = (
            "## Task\nRepair the agent-owned failure on " + tid + ".\n"
            "Read its exact request, latest failed run/log, checkpoint and acceptance. "
            "Diagnose and fix the root cause within existing authority; preserve unfinished "
            "source changes and prior effects. Do not rerun the original external action. "
            "No Harry input: this is an agent fault. If genuinely unrepairable, record the "
            "precise agent exception without asking Harry.\n\n"
            "## Failure evidence\n" + str(reason)[:1200] + "\n\n"
            "## Acceptance\nReproduce the fault, implement a repair, prove the failing "
            "transition now works, and request independent review with the actual evidence. "
            "Only verified completion reopens the original task from its checkpoint.\n"
        )
        try:
            owner = dispatch_owner(conn, cfg.get("repair_assignee"))
        except Exception as error:
            _exception(conn, tid, "repair_owner_unavailable", error=type(error).__name__)
            continue
        repair = kb.create_task(conn, title="Repair execution failure: " + row["title"][:100],
                                body=body, assignee=owner,
                                created_by="operator-repair", goal_mode=True,
                                max_runtime_seconds=runtime, max_retries=1,
                                idempotency_key="operator-repair:" + tid, board=board)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "operator_repair_created",
                             {"repair_task_id": repair, "fault_event_id": fault["id"],
                              "due_at": now + runtime, "owner": "agent"})
        kb.block_task(conn, tid, reason="Agent-owned repair " + repair + " must pass independent review before resume.", kind="dependency")
        actions.append({"task_id": tid, "repair_task_id": repair})
        pending += 1

    for event in conn.execute("SELECT e.* FROM task_events e JOIN tasks t ON t.id=e.task_id "
                              "WHERE e.kind='operator_repair_created' AND t.status='blocked'").fetchall():
        tid = event["task_id"]
        info = _payload(event)
        repair = kb.get_task(conn, info["repair_task_id"])
        if not repair:
            continue
        reviewed = _last(conn, repair.id, "review_requested")
        completed = _last(conn, repair.id, "completed")
        # A bare done flag, copied summary or self-marked repair is insufficient.
        if repair.status == "done" and reviewed and completed and completed["id"] > reviewed["id"]:
            run = conn.execute("SELECT profile FROM task_runs WHERE id=?", (completed["run_id"],)).fetchone()
            implementer = _payload(reviewed).get("implementer")
            if not run or not implementer or run["profile"] == implementer:
                continue
            if kb.unblock_task(conn, tid):
                with kb.write_txn(conn):
                    kb._append_event(conn, tid, "operator_repair_verified",
                                     {"repair_task_id": repair.id, "review_run_id": completed["run_id"]})
                actions.append({"task_id": tid, "resumed_after_repair": repair.id})
        elif now >= info["due_at"] and not _last(conn, tid, "operator_repair_overdue"):
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "operator_repair_overdue",
                                 {"repair_task_id": repair.id, "owner": "agent", "due_at": info["due_at"]})
            actions.append({"task_id": tid, "exception": "repair_deadline_exceeded"})
    return actions


def reconcile(conn, *, board=None, settings=None, now=None):
    """A faulty operator row must never stop the existing reclaim/spawn loop."""
    from hermes_cli import kanban_db as kb
    try:
        return _reconcile(conn, board=board, settings=settings, now=now)
    except Exception as error:
        kb._log.exception("operator reconciliation failed; native reclaim/spawn continues")
        return [{"exception":"reconciliation_failed", "owner":"agent", "error":type(error).__name__}]
