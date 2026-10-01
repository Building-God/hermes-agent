"""Bounded front-door turn deadline: truthful acknowledgement + durable tracked continuation.

The gateway's per-turn agent loop is bounded by an *inactivity* timeout
(``agent.gateway_timeout`` / ``HERMES_AGENT_TIMEOUT``) that only fires when the
agent goes completely idle. A turn that keeps making slow provider/tool calls
stays "active", so that watchdog never fires and a simple status/why question can
spin for 20+ minutes with no response-ready event (observed 2026-10-01 06:07:28
AEST: one Discord turn ran ``time=1349.0s api_calls=22`` before ``response ready``).

This module adds a *wall-clock* deadline that is independent of activity. When a
chat turn outlives the deadline before producing a final response, the gateway
instead:

1. journals the request durably to a JSONL file (survives gateway restart), keyed
   by an idempotency key so a restart/replay never answers the same request twice;
2. emits a truthful bounded acknowledgement naming the tracking id (never a false
   "still working" ping, never silence);
3. hands back a bounded result so the provider wait / tool loop is recovered and
   the deeper work continues as a tracked record instead of an unbounded turn.

The feature is opt-in: ``agent.gateway_turn_deadline`` (seconds, env
``HERMES_AGENT_TURN_DEADLINE``) defaults to ``0`` (disabled) so existing installs
are unchanged until an operator enables it with a measured value. This is the
safe scoped remedy for the observed failure; it does not change the global model
selection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("gateway.front_door_deadline")

# 0 = disabled. Opt-in so no existing install changes behaviour without an
# operator choosing a value (the task forbids changing defaults on guesswork).
DEFAULT_DEADLINE_SECONDS = 0.0

JOURNAL_FILENAME = "front_door_deadline_journal.jsonl"


def resolve_front_door_deadline(raw: Any, env_value: Optional[str] = None) -> float:
    """Resolve the wall-clock front-door deadline in seconds.

    Precedence: ``env_value`` (already-bridged ``HERMES_AGENT_TURN_DEADLINE``) if
    it parses as a positive float, else ``raw`` (the config.yaml
    ``agent.gateway_turn_deadline`` value), else 0. Non-positive values mean
    "disabled" and are normalised to 0.0 so callers only ever compare against a
    strict ``> 0`` deadline.
    """
    candidates = [env_value, raw]
    for value in candidates:
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0.0


def journal_path(hermes_home: Any) -> Path:
    """Absolute path of the durable deadline journal under ``hermes_home``."""
    return Path(hermes_home) / JOURNAL_FILENAME


def idempotency_key(
    platform: str, conversation_id: str, request_ts: float, message: str = "",
) -> str:
    """Stable key for one inbound request: same conversation + same second.

    Used so a gateway restart that re-delivers the same inbound message (or a
    replayed session resume) can detect that the request was already journaled and
    acknowledged, instead of answering it twice or silently dropping it.

    ``request_ts`` is the original inbound timestamp when one was persisted. When
    it was NOT persisted (``0``/``None``), the key must still be stable across a
    restart of the same message, so it falls back to a content hash of ``message``
    rather than the wall clock (which would re-key on every resume and never
    dedupe). Tradeoff: two *identical* messages that both lacked a timestamp would
    collide - but that is far rarer and safer than never deduping a resumed turn.
    """
    if request_ts:
        discriminator = str(int(request_ts))
    else:
        discriminator = "m" + hashlib.sha1((message or "").encode("utf-8")).hexdigest()[:16]
    return f"{platform}:{conversation_id}:{discriminator}"


def tracking_id(
    platform: str, conversation_id: str, request_ts: float, message: str = "",
) -> str:
    """Short stable tracking id for a request (human-readable, unique per request)."""
    key = idempotency_key(platform, conversation_id, request_ts, message)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    ts_part = str(int(request_ts)) if request_ts else "m"
    return f"fd-{ts_part}-{digest}"


def make_request_entry(
    *,
    platform: str,
    conversation_id: str,
    session_key: Optional[str],
    user: Optional[str],
    message: str,
    request_ts: float,
    deadline_seconds: float,
    ack_ts: Optional[float] = None,
    card_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the durable journal entry for one request."""
    _ts = request_ts if request_ts else time.time()
    _key = idempotency_key(platform, conversation_id, request_ts, message)
    return {
        "tracking_id": tracking_id(platform, conversation_id, request_ts, message),
        "idempotency_key": _key,
        "platform": platform,
        "conversation_id": str(conversation_id),
        "session_key": session_key,
        "user": user,
        "message": message,
        "request_ts": _ts,
        "request_ts_iso": datetime.fromtimestamp(_ts, tz=timezone.utc).isoformat(),
        "deadline_seconds": deadline_seconds,
        "ack_ts": ack_ts,
        "ack_ts_iso": (
            datetime.fromtimestamp(ack_ts, tz=timezone.utc).isoformat()
            if ack_ts is not None
            else None
        ),
        "status": "acknowledged",
        "source": "front_door_deadline",
        "card_id": card_id,
    }


def journal_request(hermes_home: Any, entry: Dict[str, Any]) -> Path:
    """Append ``entry`` to the durable journal, skipping an already-journaled key.

    Returns the journal path. The append is a single line per request; the
    idempotency check is a defensive second guard on top of the caller's
    once-per-turn flag so a restart/replay can never double-answer.
    """
    path = journal_path(hermes_home)
    key = entry.get("idempotency_key")
    if key and is_journaled(hermes_home, key):
        return path
    line = json.dumps(entry, ensure_ascii=True, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        import os
        os.fsync(fh.fileno())
    return path


def is_journaled(hermes_home: Any, key: Optional[str]) -> bool:
    """True when ``key`` already exists in the durable journal (idempotency check)."""
    if not key:
        return False
    return _journal_has_key(journal_path(hermes_home), key)


def _journal_has_key(path: Path, key: str) -> bool:
    """Best-effort scan of an existing journal for ``key`` (small file, linear ok)."""
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if existing.get("idempotency_key") == key:
                    return True
    except OSError:
        return False
    return False


def read_journal(hermes_home: Any) -> list:
    """Read every entry in the durable journal (newest last)."""
    path = journal_path(hermes_home)
    if not path.exists():
        return []
    entries = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries


def build_bounded_ack(entry: Dict[str, Any]) -> str:
    """Truthful same-thread acknowledgement for a request that outlived its deadline.

    This is a *truthful* ack, not a "still working" ping: it says the request is
    captured and tracked under a specific id, that the answer is being worked in
    the background, and where it will land. It deliberately does NOT claim a
    human has read anything.
    """
    task_id = entry.get("card_id")
    if not task_id:
        return "The reply timed out. I could not confirm a background owner; the request is retained for automatic intake recovery. Completion is not confirmed."
    return f"This needs a longer answer. `{task_id}` owns it and will return the result here."


def build_resume_reference(entry: Dict[str, Any]) -> str:
    """Short truthful reference for a resumed turn whose request was already acked.

    Used when a gateway restart re-runs the same interrupted turn and it hits the
    deadline again: we must NOT re-answer or send a duplicate ack, and must NOT ask
    the user to resend. This is a one-line status reference, not a new answer.
    """
    if entry.get("card_id"):
        return f"Existing task `{entry['card_id']}` holds this request; its result will return here."
    return build_bounded_ack(entry)


def ensure_continuation(entry: Dict[str, Any], *, board: Optional[str] = None, home: Any = None,
                        assignee: Optional[str] = None, notifier_profile: Optional[str] = None) -> str:
    """A deadline receipt is not execution. Atomically persist an owned task and
    its exact return route before promising continuation. Replay returns the same
    task; an existing task from this inbound message is reused, never duplicated.
    This uses the existing board, dispatcher and notifier, not another work queue.
    """
    from hermes_cli import kanban_db as kb

    platform = entry.get("platform", "")
    user_id = entry.get("user_id", "")
    chat_id = entry.get("conversation_id", "")
    message_id = entry.get("message_id") or entry["idempotency_key"]
    message = entry.get("message", "")
    if not all((platform, user_id, chat_id, message)):
        raise ValueError("continuation requires authenticated origin and return route")
    body = (
        "## Harry's request\n" + message + "\n\n## Task\n"
        "Finish the interrupted request within its existing authority. Inspect any prior "
        "task/effects before acting; do not repeat external effects. Preserve current project "
        "policy and unfinished changes. Use existing tools and delegate when appropriate. "
        "Repair agent-owned failures; only a genuinely human-only choice may ask Harry.\n\n"
        "## Acceptance\nIndependently verify the requested outcome against the original "
        "request, preserve evidence/artifacts, and request independent review where needed. "
        "Return a substantive answer via kanban_complete result/summary; the notifier must "
        "deliver to the original conversation. Transport send and Harry receipt remain "
        "separate. A created card, journal or heartbeat is not completion.\n"
    )
    from hermes_cli.kanban_db_connect import connect
    target = None
    if home is not None:
        slug = board or kb.get_current_board()
        target = (Path(home) / "kanban.db" if slug == "default" else
                  Path(home) / "kanban" / "boards" / slug / "kanban.db")
    conn = connect(target, board=board)
    try:
        from hermes_cli.kanban_operator import dispatch_owner
        assignee = dispatch_owner(conn, assignee)
        with kb.write_txn(conn):
            task_id = kb.create_task(
                conn, title="Finish interrupted request: " + message.splitlines()[0][:100],
                body=body, assignee=assignee, created_by="front-door-continuation",
                idempotency_key="front-door:" + entry["idempotency_key"],
                max_runtime_seconds=600, max_retries=2,
                goal_mode=True, initial_status="blocked" if entry.get("foreground_fenced") else "running",
                user_origin={"platform": platform, "chat_id": str(chat_id),
                             "message_id": str(message_id), "user_id": str(user_id), "text": message},
            )
            if entry.get("foreground_fenced") and kb.get_task(conn,task_id).created_by == "front-door-continuation":
                if not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='operator_frontdoor_fence'",(task_id,)).fetchone():
                    conn.execute("UPDATE tasks SET block_kind='dependency' WHERE id=? AND status='blocked'",(task_id,))
                    kb._append_event(conn,task_id,"operator_frontdoor_fence",{"owner":"agent", "due_at":int(time.time())+300})
            kb.add_notify_sub(conn, task_id=task_id, platform=platform, chat_id=str(chat_id),
                              thread_id=entry.get("thread_id"), user_id=str(user_id),
                              notifier_profile=notifier_profile, delivery_mode="notify",
                              delivery_metadata={"reply_to_message_id": str(message_id)})
        entry["card_id"] = task_id
        return task_id
    finally:
        conn.close()


def recover_unowned_intake(home, *, board=None):
    """Retry existing deadline intake escrow in the existing dispatcher, not a watcher.

    Owned rows never execute here. Creation is idempotent by authenticated origin.
    An append receipt permits restart reconciliation without rewriting other turns.
    """
    import os
    latest = {row.get("idempotency_key"): row for row in read_journal(home) if isinstance(row, dict)}
    results = []
    for key, row in latest.items():
        if not key or row.get("card_id") or row.get("recovery_board") not in (None, board):
            continue
        if not all(row.get(field) for field in ("platform", "user_id", "conversation_id", "message")):
            continue  # No authenticated authority: never fabricate a task origin.
        try:
            ensure_continuation(row, home=home, board=board)
        except Exception as error:
            logger.warning("intake recovery retained %s: %s", key, type(error).__name__)
            results.append({"key":key,"owner":"agent","error":type(error).__name__})
            continue
        newest=next((saved for saved in reversed(read_journal(home)) if saved.get("idempotency_key")==key),{})
        if newest.get("foreground_stopped"):
            row["foreground_stopped"]=True
            row["foreground_fenced"]=False
            settle_foreground_fence(row,home,None)
        row["status"] = "owned_after_intake_recovery"
        with journal_path(home).open("a",encoding="utf-8") as handle:
            handle.write(json.dumps(row,ensure_ascii=True,sort_keys=True)+"\n")
            handle.flush(); os.fsync(handle.fileno())
        results.append({"key":key,"card_id":row["card_id"]})
    return results


def settle_foreground_fence(entry, home, worker_done):
    """Do not dispatch replacement work beside an interrupted foreground worker."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    stopped = worker_done.wait(300) if worker_done is not None else bool(entry.get("foreground_stopped"))
    slug = entry.get("recovery_board") or kb.get_current_board()
    target = Path(home)/"kanban.db" if slug == "default" else Path(home)/"kanban/boards"/slug/"kanban.db"
    conn=connect(target)
    try:
        tid=entry.get("card_id")
        if not tid:
            latest=next((row for row in reversed(read_journal(home)) if row.get("idempotency_key")==entry["idempotency_key"]),entry)
            tid=latest.get("card_id")
            if tid:
                entry["card_id"]=tid
        if stopped:
            entry["foreground_fenced"]=False
            entry["foreground_stopped"]=True
            entry["status"]="foreground_stopped"
            with journal_path(home).open("a",encoding="utf-8") as handle:
                handle.write(json.dumps(entry,ensure_ascii=True,sort_keys=True)+"\n")
                handle.flush()
        if not tid:
            return
        fence=conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='operator_frontdoor_fence'",(tid,)).fetchone()
        if not fence:
            return
        if stopped:
            with kb.write_txn(conn):
                kb._append_event(conn,tid,"operator_frontdoor_stopped",{"owner":"agent","original_worker_done":True})
            kb.unblock_task(conn,tid)
        else:
            with kb.write_txn(conn):
                kb._append_event(conn,tid,"operator_exception",{"owner":"agent","reason":"foreground_shutdown_unconfirmed","duplicate_execution_fenced":True})
    finally:
        conn.close()
