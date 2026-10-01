"""Closed-loop reconciliation within the existing board/dispatcher.

No additional scheduler or runnable store. All deadlines, repairs and exceptions
are durable events on the original task. Human choices are never inferred from
worker faults. Repairs are finite, independently reviewed existing-board tasks.
"""
from __future__ import annotations

import json
import re
import hashlib
from pathlib import Path
import time


def _last_hold(conn,task_id):
    return conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind IN ('blocked','block_loop_detected','dependency_wait') ORDER BY id DESC LIMIT 1",(task_id,)).fetchone()


def repair_authority(conn, repair_id):
    """Resolve a repair's original from durable native provenance, never its prose."""
    rows = conn.execute("SELECT * FROM task_events WHERE kind='operator_repair_created' AND CASE WHEN json_valid(payload) THEN json_extract(payload,'$.repair_task_id') END=?",(repair_id,)).fetchall()
    if len(rows) != 1:
        return None
    event = rows[0]
    original = conn.execute("SELECT * FROM tasks WHERE id=?",(event['task_id'],)).fetchone()
    origin = conn.execute("SELECT * FROM task_user_origins WHERE task_id=?",(event['task_id'],)).fetchone()
    return {'event':event,'original':original,'origin':origin}


def repair_descendant_hold(conn,repair_id,reason):
    from hermes_cli import kanban_db as kb
    return any(ref!=repair_id and kb._would_cycle(conn,ref,repair_id) for ref in set(re.findall(r'\bt_[a-zA-Z0-9]+\b',str(reason or ''))))


def verified_repair_for_original(conn,original_id):
    event=_last(conn,original_id,'operator_repair_created')
    if not event:
        return None
    repair_id=_payload(event).get('repair_task_id')
    repair=conn.execute('SELECT status FROM tasks WHERE id=?',(repair_id,)).fetchone()
    completed=_last(conn,repair_id,'completed');review=_last(conn,repair_id,'review_requested')
    if not repair or repair['status']!='done' or not completed or not review or completed['id']<=review['id']:
        return None
    payload=_payload(completed);contract=payload.get('operator_repair_contract') or {}
    implementer=_payload(review).get('implementer')
    run=conn.execute('SELECT profile FROM task_runs WHERE id=?',(completed['run_id'],)).fetchone()
    claim=conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed' ORDER BY id DESC LIMIT 1",(repair_id,completed['run_id'])).fetchone()
    if not implementer or not isinstance(contract,dict) or contract.get('original_task_id')!=original_id or contract.get('owner')!='agent' or not payload.get('acceptance_receipts') or not run or run['profile']==implementer or not claim or json.loads(claim['payload'] or '{}').get('source_status')!='review':
        return None
    return {'repair_task_id':repair_id,'review_run_id':completed['run_id']}


def repair_contract(conn, repair_id):
    """Machine check the owned transition; this is not functional acceptance."""
    authority=repair_authority(conn,repair_id)
    if authority is None:
        return None,None
    original=authority['original']; event=authority['event']
    changes=_last(conn,repair_id,'changes_requested')
    review=_last(conn,repair_id,'review_requested')
    boundary=max(changes['id'] if changes else event['id'],review['id'] if review else event['id'])
    drift=conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='operator_repair_hold_reconciled' AND id>? ORDER BY id DESC LIMIT 1",(event['task_id'],boundary)).fetchone()
    if original is None or original['status']!='blocked' or original['block_kind']!='dependency' or original['claim_lock'] is not None or drift:
        return None,('Repair failed its native authority contract: keep the original agent-owned dependency hold until independent acceptance. '
                     'Do not relabel it needs_input or edit its row directly. Reproduce and repair the original requested outcome; '
                     'changing ownership flags or asking Harry for deployment/review is not acceptance.')
    return {'original_task_id':event['task_id'],'fault_event_id':_payload(event).get('fault_event_id'),
            'origin_message_id':authority['origin']['message_id'] if authority['origin'] else None,
            'owner':'agent','checks':['original_hold_intact','no_execution_claim','no_hold_drift_this_candidate']},None


def agent_answer_claim_failure(conn,task_id,text):
    """Reject the observed counterexample: agent taps promoted to Harry proof.

    This guards explicit Harry confirmation/testing claims, not all prose truth.
    Transport receipt remains a separate authenticated result-reply event.
    """
    prose=re.sub(r"\bHarry(?:'s)?\s+(?:authenticated\s+)?confirmation\s+(?:(?:is|remains|was)\s+)?(?:unobserved|unproved|unverified|missing)\b",'',str(text or ''),flags=re.I)
    prose=re.sub(r"\b(?:no|without)\s+(?:authenticated\s+)?Harry(?:'s)?\s+confirmation\b",'',prose,flags=re.I)
    if not re.search(r"\bHarry(?:'s)?\s+(?:authenticated\s+)?(?:confirm(?:ed|ation)|tested|said|answered)\b",prose,re.I):
        return None
    comments=conn.execute("SELECT body FROM task_comments WHERE task_id=? AND (body LIKE 'AGENT[%' OR body LIKE 'HARRY[%')",(task_id,)).fetchall()
    agent_questions=set()
    for row in comments:
        if row['body'].startswith('AGENT['):
            agent_questions.update(re.findall(r'q_[a-zA-Z0-9]+',row['body']))
    human_questions={qid for row in comments for qid in re.findall(r'^HARRY\[(q_[a-zA-Z0-9]+)\]:',row['body'])}
    if agent_questions - human_questions:
        return ('False human acceptance: this task has agent-labeled question answers and no authenticated Harry confirmation. '
                'Do not promote an AGENT tap, elapsed time, successful send or stored answer into Harry proof. '
                'Retain objective reproduction and correct the result; human acceptance and result receipt stay unobserved.')
    return None


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


def acceptance_owner(conn,cfg,current_owner):
    for candidate in (cfg.get('acceptance_recheck_assignee','architect'),cfg.get('review_assignee','reviewer'),'reviewer','pilot','architect'):
        try:
            owner=dispatch_owner(conn,candidate)
            if owner!=current_owner:return owner
        except (ValueError,RuntimeError):
            continue
    raise ValueError('No different installed reviewer is available for corrective acceptance')


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
    # A repair cannot depend on the failed original that is waiting for it.
    # Correct only the reverse edge identified by durable native repair origin.
    for event in conn.execute("SELECT * FROM task_events WHERE kind='operator_repair_created'").fetchall():
        original_id=event['task_id'];repair_id=_payload(event).get('repair_task_id')
        if not repair_id:continue
        original=kb.get_task(conn,original_id);repair=kb.get_task(conn,repair_id)
        origin=conn.execute('SELECT 1 FROM task_user_origins WHERE task_id=?',(original_id,)).fetchone()
        reverse=conn.execute('SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?',(original_id,repair_id)).fetchone()
        if not origin or not reverse or not original or not repair or original.status!='blocked' or original.block_kind!='dependency' or original.claim_lock or repair.claim_lock:
            continue
        try:
            with kb.write_txn(conn):
                authority=repair_authority(conn,repair_id)
                if not authority or authority['event']['id']!=event['id']:continue
                conn.execute('DELETE FROM task_links WHERE parent_id=? AND child_id=?',(original_id,repair_id))
                if kb._would_cycle(conn,repair_id,original_id):
                    raise ValueError('Correct repair prerequisite still creates a cycle; retain original edge and agent exception')
                conn.execute('INSERT OR IGNORE INTO task_links(parent_id,child_id) VALUES(?,?)',(repair_id,original_id))
                kb._append_event(conn,original_id,'operator_repair_dependency_corrected',{'owner':'agent','repair_task_id':repair_id,'source_event_id':event['id'],'removed_edge':[original_id,repair_id],'added_edge':[repair_id,original_id]})
            actions.append({'task_id':original_id,'repair_prerequisite_corrected':repair_id})
        except Exception as error:
            _exception(conn,original_id,'repair_prerequisite_correction_failed',error=str(error)[:300])

    # Archival is not acceptance. Recover the exact failed terminal transition
    # without erasing its evidence or replaying the original action.
    for tid in cfg.get('cohort_task_ids',[]):
        task=kb.get_task(conn,tid);completed=_last(conn,tid,'completed');invalid=_last(conn,tid,'operator_acceptance_invalidated')
        failed_acceptance=(task and task.status=='blocked' and task.block_kind=='capability' and _last(conn,tid,'operator_acceptance_stopped'))
        if not task or (task.status!='archived' and not failed_acceptance) or not completed or _last(conn,tid,'operator_terminal_review_handoff'):
            continue
        from hermes_cli.kanban_acceptance_truth import completion_failure
        declared=(cfg.get('agent_owned_faults') or {}).get(tid,{})
        failure=completion_failure(conn,tid,task.result or '',cfg)
        if declared.get('completed_event_id')==completed['id'] and declared.get('source') and declared.get('reason'):
            failure='Audit-proved invalid completion: '+str(declared['reason'])
        if (not invalid or invalid['id']<=completed['id']) and not failure:
            continue
        with kb.write_txn(conn):
            changed=conn.execute("UPDATE tasks SET status='blocked',block_kind='capability' WHERE id=? AND status=? AND claim_lock IS NULL",(tid,task.status)).rowcount
            if changed:
                if not invalid or invalid['id']<=completed['id']:
                    kb._append_event(conn,tid,'operator_acceptance_invalidated',{'owner':'agent','source_completed_event_id':completed['id'],'reason':failure})
                kb._append_event(conn,tid,'operator_terminal_recovered',{'owner':'agent','source_completed_event_id':completed['id'],'reason':'Invalidated result lacks corrected acceptance/delivery after terminal failure','previous_status':task.status})
                kb._append_event(conn,tid,'blocked',{'owner':'agent','kind':'capability','reason':'Agent-owned terminal acceptance failure; original request remains owned.'})
    for tid in cfg.get('cohort_task_ids',[]):
        task=kb.get_task(conn,tid);terminal=_last(conn,tid,'operator_terminal_recovered')
        if not task or task.status!='blocked' or task.claim_lock or not terminal or _last(conn,tid,'operator_terminal_review_handoff'):
            continue
        try:
            reviewer=acceptance_owner(conn,cfg,task.assignee)
            ok,reason=kb.request_review(conn,tid,reviewer=reviewer,summary='Correct the invalidated original result and verify existing objective effects. Archival cannot bypass acceptance/delivery. Do not replay actions or claim an AGENT answer was Harry confirmation. Keep subjective confirmation and human result receipt explicitly separate.',metadata={'acceptance_receipts':{'native_terminal_correction':{'terminal_event_id':terminal['id'],'source_completed_event_id':_payload(terminal)['source_completed_event_id'],'invalidated_event_id':_last(conn,tid,'operator_acceptance_invalidated')['id'],'scope':'Actual invalidation and terminal state; functional acceptance remains unproved'}}},resume_failed_terminal=True,with_reason=True)
            if not ok:raise ValueError(reason or 'terminal recovery review refused')
            with kb.write_txn(conn):
                kb._append_event(conn,tid,'operator_terminal_review_handoff',{'owner':'agent','source_terminal_event_id':terminal['id'],'due_at':now+runtime,'reviewer':reviewer})
                kb._append_event(conn,tid,'operator_acceptance_recovery',{'owner':'agent','source_terminal_event_id':terminal['id'],'due_at':now+runtime,'reviewer':reviewer,'acceptance':'unproved'})
            actions.append({'task_id':tid,'terminal_acceptance_recovered':True})
        except Exception as error:_exception(conn,tid,'terminal_acceptance_recovery_failed',error=str(error)[:300])
    try:
        from hermes_cli.kanban_release_entrypoint import reconcile_entrypoint
        release_action=reconcile_entrypoint(cfg.get('release_entrypoint'),now=now)
        if release_action:
            actions.append(release_action)
    except Exception as error:
        actions.append({'exception':'release_entry_reconciliation_failed','owner':'agent','error':type(error).__name__})
    # Reconcile the observed false acceptance even when an earlier audit phase
    # completed. One corrective review, bounded independently, preserves effects.
    for tid in cfg.get('cohort_task_ids',[]):
        task=kb.get_task(conn,tid)
        completed=_last(conn,tid,'completed')
        if not task or task.status!='done' or task.claim_lock or not completed or _last(conn,tid,'operator_acceptance_invalidated'):
            continue
        from hermes_cli.kanban_acceptance_truth import completion_failure
        failure=completion_failure(conn,tid,str(task.result or '')+' '+str(_payload(completed).get('summary','')),cfg)
        declared=(cfg.get('agent_owned_faults') or {}).get(tid,{})
        if not failure and declared.get('completed_event_id')==completed['id'] and declared.get('source') and declared.get('reason'):
            failure='Audit-proved invalid completion: '+str(declared['reason'])
        if not failure:
            continue
        try:
            reviewer=acceptance_owner(conn,cfg,task.assignee)
            if reviewer==task.assignee:
                raise ValueError('corrective acceptance reviewer must differ from the false-claim actor')
            ok,reason=kb.request_review(conn,tid,reviewer=reviewer,summary=failure+' Independently verify objective outcome and issue a corrected substantive result in the original conversation. Do not repeat external effects.',resume_audited_origin=True,with_reason=True)
            if not ok:
                raise ValueError(reason or 'native corrective review refused')
            with kb.write_txn(conn):
                kb._append_event(conn,tid,'operator_acceptance_invalidated',{'owner':'agent','source_completed_event_id':completed['id'],'reason':failure,'reviewer':reviewer})
                kb._append_event(conn,tid,'operator_acceptance_recovery',{'owner':'agent','source_completed_event_id':completed['id'],'due_at':now+runtime,'reviewer':reviewer,'acceptance':'unproved','corrective_review':True})
            actions.append({'task_id':tid,'false_acceptance_recheck':reviewer})
        except Exception as error:
            _exception(conn,tid,'false_acceptance_recovery_failed',error=str(error)[:300])
    # Audit-selected legacy outcomes cannot stay green without acceptance, and
    # an exact diagnosed deployment fault is not a new human choice. This is a
    # single native reviewer handoff; no original external action is replayed.
    for tid in cfg.get("cohort_task_ids",[]):
        row=kb.get_task(conn,tid)
        if not row or row.claim_lock or _last(conn,tid,"operator_acceptance_recovery"):
            continue
        completed=_last(conn,tid,"completed")
        declared=(cfg.get("agent_owned_faults") or {}).get(tid,{})
        current_block=_last_hold(conn,tid)
        if row.status in ('blocked','triage') and (not current_block or declared.get("blocked_event_id")!=current_block["id"]):
            continue  # A later human choice invalidates the old diagnosis.
        if not ((row.status=='done' and completed and (not row.result or not _payload(completed).get("acceptance_receipts"))) or
                (row.status in ('blocked','triage') and declared)):
            continue
        try:
            reviewer=dispatch_owner(conn,cfg.get("review_assignee","reviewer"))
            summary="Audit acceptance against the exact authenticated original request and existing evidence. Do not repeat prior external effects. Independently reproduce the requested outcome, retain a substantive result and actual acceptance receipts, and separate objective delivery from unobserved Harry receipt or subjective confirmation. " + str(declared.get("reason","Legacy completion has no substantive independently verified result."))
            accepted,reason=kb.request_review(conn,tid,reviewer=reviewer,summary=summary,resume_audited_origin=True,with_reason=True)
            if not accepted:
                raise ValueError(reason or "native origin review refused")
            with kb.write_txn(conn):
                kb._append_event(conn,tid,"operator_acceptance_recovery",{"owner":"agent","reviewer":reviewer,"source_completed_event_id":completed["id"] if completed else None,"source_blocked_event_id":declared.get("blocked_event_id"),"due_at":now+runtime,"acceptance":"unproved"})
            actions.append({"task_id":tid,"acceptance_recovery":reviewer})
        except Exception as error:
            _exception(conn,tid,"acceptance_recovery_failed",error=str(error)[:300])
    for event in conn.execute("SELECT e.* FROM task_events e JOIN tasks t ON t.id=e.task_id WHERE e.kind='operator_acceptance_recovery' AND t.status IN ('review','running') AND e.id=(SELECT MAX(latest.id) FROM task_events latest WHERE latest.task_id=e.task_id AND latest.kind='operator_acceptance_recovery')").fetchall():
        info=_payload(event);tid=event["task_id"]
        if now < info["due_at"]:
            continue
        _exception(conn,tid,"acceptance_review_deadline_exceeded",due_at=info["due_at"])
        task=kb.get_task(conn,tid)
        if task.status=='running':
            stopped=kb.block_task(conn,tid,reason="Agent-owned acceptance review deadline exhausted.",kind='capability')
        else:
            with kb.write_txn(conn):
                stopped=conn.execute("UPDATE tasks SET status='blocked',block_kind='capability' WHERE id=? AND status='review' AND claim_lock IS NULL",(tid,)).rowcount > 0
        if stopped:
            with kb.write_txn(conn):
                kb._append_event(conn,tid,'operator_acceptance_stopped',{'owner':'agent','due_at':info['due_at']})
                kb._append_event(conn,tid,"gave_up",{"owner":"agent","error":"Acceptance review deadline exhausted","source_event_id":event["id"]})
            actions.append({"task_id":tid,"acceptance_review_stopped":True})
    # The live pilot mislabeled reviewer approval as Harry input. Repairs are
    # provably agent-owned: hand existing candidate evidence to a real reviewer,
    # never approve it or restart implementation merely to escape the hold.
    # Exact independently audited historical repair faults get one ownership
    # correction review. This verifies authority only, never functional success.
    for repair_id,audit in (cfg.get('audited_repair_faults') or {}).items():
        task=kb.get_task(conn,repair_id)
        if not task or task.claim_lock or task.status not in ('blocked','todo') or _last(conn,repair_id,'operator_authority_review_handoff'):
            continue
        contract,failure=repair_contract(conn,repair_id)
        if failure or not contract:continue
        try:
            reviewer=acceptance_owner(conn,cfg,task.assignee)
            metadata={'acceptance_receipts':{'native_authority_correction':{'audit_event_id':audit.get('event_id'),'contract':contract,'scope':'Ownership only; requested functional result remains unproved'}}}
            ok,reason=kb.request_review(conn,repair_id,reviewer=reviewer,summary=str(audit.get('reason',''))+' Independently reproduce the authenticated original requested outcome. Deployment and routing are agent-owned. This machine receipt verifies ownership only, not functional success; retain actual probe receipts and do not ask Harry to repair agent authority.',metadata=metadata,resume_audited_repair=True,with_reason=True)
            if not ok:raise ValueError(reason or 'audited repair correction refused')
            with kb.write_txn(conn):
                kb._append_event(conn,repair_id,'operator_authority_review_handoff',{'owner':'agent','audit_event_id':audit.get('event_id'),'due_at':now+runtime,'reviewer':reviewer})
                kb._append_event(conn,repair_id,'operator_agent_review_handoff',{'owner':'agent','source_event_id':audit.get('event_id'),'due_at':now+runtime,'reviewer':reviewer,'acceptance':'unproved'})
            actions.append({'task_id':repair_id,'audited_authority_recovery':reviewer})
        except Exception as error:
            _exception(conn,repair_id,'audited_authority_recovery_failed',error=str(error)[:300])
    for row in conn.execute("SELECT * FROM tasks WHERE created_by='operator-repair' AND status='blocked' AND (block_kind='dependency' OR block_kind IS NULL) AND claim_lock IS NULL").fetchall():
        tid=row['id'];blocked=_last_hold(conn,tid)
        if _last(conn,tid,'operator_agent_dependency_review_handoff') or not blocked or not repair_descendant_hold(conn,tid,_payload(blocked).get('reason')):
            continue
        try:
            reviewer=acceptance_owner(conn,cfg,row['assignee'])
            accepted,reason=kb.request_review(conn,tid,reviewer=reviewer,summary='Circular review hold is agent-owned. A verification descendant cannot be this repair prerequisite. Review the original authenticated outcome and native ownership contract; do not ask Harry or accept relabeling agent work as needs_input.',metadata={'acceptance_receipts':{'native_authority':{'contract':repair_contract(conn,tid)[0],'source_blocked_event_id':blocked['id'],'scope':'Ownership only; functional acceptance unproved'}}},resume_agent_repair=True,with_reason=True)
            if not accepted:
                raise ValueError(reason or 'native circular review recovery refused')
            with kb.write_txn(conn):
                kb._append_event(conn,tid,'operator_agent_dependency_review_handoff',{'owner':'agent','source_blocked_event_id':blocked['id'],'reviewer':reviewer,'due_at':now+runtime,'acceptance':'unproved'})
                kb._append_event(conn,tid,'operator_agent_review_handoff',{'owner':'agent','source_event_id':blocked['id'],'reviewer':reviewer,'due_at':now+runtime,'corrective_review':True,'acceptance':'unproved'})
            actions.append({'task_id':tid,'circular_review_recovered':reviewer})
        except Exception as error:
            _exception(conn,tid,'circular_review_recovery_failed',error=str(error)[:300])
    for row in conn.execute("SELECT * FROM tasks WHERE created_by='operator-repair' AND status='blocked' AND block_kind='needs_input' AND claim_lock IS NULL").fetchall():
        tid=row["id"]; blocked=_last(conn,tid,"blocked")
        try:
            reviewer=dispatch_owner(conn,cfg.get("review_assignee","reviewer"))
            if reviewer==row["assignee"]:
                raise ValueError("repair reviewer must differ from implementer")
            workspace=Path(row["workspace_path"]) if row["workspace_path"] else None
            database=Path(next(r[2] for r in conn.execute("PRAGMA database_list") if r[1]=='main'))
            artifacts=[]
            if workspace and workspace.resolve()==(database.parent/"workspaces"/tid).resolve():
                for name in ("REPAIR_EVIDENCE.md","VERIFICATION_OUTPUT.txt"):
                    path=workspace/name
                    if path.is_file() and path.resolve().parent==workspace.resolve():
                        artifacts.append(str(path))
            summary="Agent-owned independent review: verify the repair against the original request and authority. Do not accept relabeling agent deployment or review as Harry input. Prior implementation run/comments are candidate evidence. " + str(_payload(blocked).get("reason", ""))
            accepted,reason=kb.request_review(conn,tid,summary=summary,reviewer=reviewer,metadata={"artifacts":artifacts,"_explicit_artifacts":list(artifacts)} if artifacts else None,with_reason=True,resume_agent_repair=True)
            if not accepted:
                raise ValueError(reason or "native review handoff refused")
            with kb.write_txn(conn):
                kb._append_event(conn,tid,"operator_agent_review_handoff",{"owner":"agent","reviewer":reviewer,"source_event_id":blocked["id"] if blocked else None,"due_at":now+runtime,"acceptance":"unproved"})
            actions.append({"task_id":tid,"agent_review_handoff":reviewer})
        except Exception as error:
            _exception(conn,tid,"agent_review_handoff_failed",error=str(error)[:300])
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET block_kind='capability' WHERE id=? AND status='blocked' AND block_kind='needs_input' AND claim_lock IS NULL",(tid,))
                kb._append_event(conn,tid,"operator_agent_hold_reconciled",{"owner":"agent","reason":"independent review handoff failed; Harry does not own this repair"})
    # Repair the exact historical auto-rekind bug; explicit needs_input is never guessed away.
    for row in conn.execute("SELECT * FROM tasks WHERE status IN ('blocked','triage') AND claim_lock IS NULL").fetchall():
        tid=row["id"]
        if _last(conn,tid,"operator_repair_created"):
            continue
        event=_last(conn,tid,"blocked"); payload=_payload(event)
        if not (payload.get("dependency_unresolved") or
                (payload.get("requested_kind")=="dependency" and payload.get("rekind_reason")=="no_open_parent")):
            continue
        if event["created_at"] < int(cfg.get("activation_at",0)) and tid not in cfg.get("cohort_task_ids",[]):
            continue
        if _last(conn,tid,"operator_dependency_reconciled") and _last(conn,tid,"operator_dependency_reconciled")["id"] > event["id"]:
            continue
        candidates=sorted(set(re.findall(r"\bt_[a-f0-9]{8}\b",payload.get("reason", "")))-{tid})
        parents=[parent for parent in candidates if kb.get_task(conn,parent) is not None]
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked',block_kind='dependency' WHERE id=? AND claim_lock IS NULL",(tid,))
        try:
            if len(parents)!=1:
                raise ValueError("missing or ambiguous formal agent dependency")
            kb.link_tasks(conn,parents[0],tid)  # Native cycle guard remains authoritative.
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='todo',block_kind='dependency' WHERE id=? AND status='blocked' AND claim_lock IS NULL",(tid,))
                kb._append_event(conn,tid,"operator_dependency_reconciled",{"owner":"agent","source_event_id":event["id"],"parent_task_id":parents[0]})
            actions.append({"task_id":tid,"dependency_reconciled":parents})
        except ValueError as error:
            if not _last(conn,tid,"operator_dependency_fault"):
                with kb.write_txn(conn):
                    kb._append_event(conn,tid,"operator_dependency_fault",{"owner":"agent","error":str(error),"source_event_id":event["id"],"referenced_tasks":parents,"due_at":now+runtime})
            _exception(conn,tid,"agent_dependency_unresolved",source_event_id=event["id"])

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
        for row in conn.execute("SELECT t.id,t.status,t.current_run_id,r.started_at AS run_started_at FROM tasks t "
                                "LEFT JOIN task_runs r ON r.id=t.current_run_id "
                                "WHERE t.status IN ('ready','review','running') AND (t.max_runtime_seconds IS NULL OR t.max_runtime_seconds > ?)", (runtime,)).fetchall():
            if row["status"] == "running" and cfg.get("activation_at") and (row["run_started_at"] or 0) < int(cfg["activation_at"]):
                continue  # Existing attempts did not receive the new deadline contract.
            conn.execute("UPDATE tasks SET max_runtime_seconds=? WHERE id=?", (runtime, row["id"]))
            conn.execute("UPDATE task_runs SET max_runtime_seconds=? WHERE id=? AND status='running'",
                         (runtime, row["current_run_id"]))
            kb._append_event(conn, row["id"], "operator_attempt_budget", {"seconds": runtime}, run_id=row["current_run_id"])

    rows = conn.execute("SELECT t.* FROM tasks t JOIN task_user_origins o ON o.task_id=t.id "
                        "WHERE t.status NOT IN ('archived')").fetchall()
    for row in rows:
        tid = row["id"]
        floor = int(cfg.get("activation_at", 0))
        if int(row["created_at"]) < floor and tid not in cfg.get("cohort_task_ids", []):
            continue  # Historical exceptions stay in the audit; do not flood old routes at rollout.
        if row["status"] in ("ready","review") and int(row["priority"] or 0) == 0:
            # Default-priority original work must not sit behind internally generated backlog.
            kb.edit_task(conn,tid,priority=int(cfg.get("user_work_priority",100)))
            with kb.write_txn(conn):
                kb._append_event(conn,tid,"operator_user_priority",{"owner":"agent","priority":int(cfg.get("user_work_priority",100))})
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
        acceptance_phase=_last(conn,tid,'operator_acceptance_recovery')
        bounded_acceptance=acceptance_phase and row['status'] in ('review','running') and now<_payload(acceptance_phase).get('due_at',0)
        if now>=due and row['status'] in ('ready','running','review') and not bounded_acceptance:
            if row['status']=='running':
                stopped=kb.block_task(conn,tid,reason='Agent-owned original request deadline exhausted; do not start another external action.',kind='capability')
            else:
                with kb.write_txn(conn):
                    stopped=conn.execute("UPDATE tasks SET status='blocked',block_kind='capability' WHERE id=? AND status=? AND claim_lock IS NULL",(tid,row['status'])).rowcount>0
            if stopped:
                with kb.write_txn(conn):
                    kb._append_event(conn,tid,'operator_request_stopped',{'owner':'agent','due_at':due})
                    kb._append_event(conn,tid,'gave_up',{'owner':'agent','error':'Original request deadline exhausted','operator_rework':True,'sticky':True})
                actions.append({'task_id':tid,'original_deadline_stopped':True})

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
                conn.execute("UPDATE tasks SET status='blocked',block_kind='capability' WHERE id=? AND claim_lock IS NULL",(row["id"],))
                kb._append_event(conn,row['id'],'operator_repair_stopped',{'owner':'agent','due_at':_payload(event)['due_at'],'phase':'rework'})
                if not _last(conn,row["id"],"gave_up") or _last(conn,row["id"],"gave_up")["id"] < event["id"]:
                    kb._append_event(conn,row["id"],"gave_up",{"error":"rework deadline exceeded", "operator_rework":True,"sticky":True})
        else:
            # Active workers retain the identity fence; the native tree enforcer stops them.
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET max_runtime_seconds=MIN(COALESCE(max_runtime_seconds,?),?) WHERE id=?",(runtime,runtime,row["id"]))

    # Exhausted automatic attempts need a repair owner, not endless blind retries
    # or an invented Harry choice. Respect sticky auth/explicit stop fences.
    pending = conn.execute("SELECT COUNT(*) FROM tasks WHERE created_by='operator-repair' AND status NOT IN ('done','archived','blocked')").fetchone()[0]
    for row in conn.execute("SELECT * FROM tasks WHERE status='blocked' AND (block_kind IS NULL OR block_kind IN ('dependency','capability')) "
                            "AND claim_lock IS NULL ORDER BY created_at DESC").fetchall():
        if pending >= int(cfg.get("max_pending_repairs", 1)):
            break
        tid = row["id"]
        if row['block_kind']=='capability' and not (_last(conn,tid,'operator_request_stopped') or _last(conn,tid,'operator_acceptance_stopped')):
            continue # Do not reinterpret unrelated auth/capability fences.
        fault = _last(conn, tid, "operator_dependency_fault") or _last(conn, tid, "gave_up")
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
            "The reviewer must record actual reproduction/probe/source receipts in metadata.acceptance_receipts. "
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
                              "WHERE e.kind='operator_repair_created' AND t.status!='archived'").fetchall():
        tid = event["task_id"]
        info = _payload(event)
        repair = kb.get_task(conn, info["repair_task_id"])
        if not repair:
            continue
        # Repair workers have direct terminal tools. Reconcile accidental raw
        # row edits with the durable hold instead of trusting a relabeled flag.
        if not _last(conn,tid,"operator_repair_verified"):
            original=kb.get_task(conn,tid)
            if original.claim_lock is not None:
                if not kb.block_task(conn,tid,reason="Agent-owned repair "+repair.id+" requires independent acceptance before execution.",kind="dependency"):
                    _exception(conn,tid,"repair_hold_worker_stop_unproved",repair_task_id=repair.id)
                original=kb.get_task(conn,tid)
            if original.claim_lock is None and (original.status!='blocked' or original.block_kind!='dependency'):
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET status='blocked',block_kind='dependency' WHERE id=? AND claim_lock IS NULL",(tid,))
                    kb._append_event(conn,tid,"operator_repair_hold_reconciled",{"owner":"agent","repair_task_id":repair.id,"previous_status":original.status,"previous_block_kind":original.block_kind})
        reviewed = _last(conn, repair.id, "review_requested")
        completed = _last(conn, repair.id, "completed")
        # A bare done flag, copied summary or self-marked repair is insufficient.
        if repair.status == "done" and reviewed and completed and completed["id"] > reviewed["id"]:
            run = conn.execute("SELECT profile FROM task_runs WHERE id=?", (completed["run_id"],)).fetchone()
            implementer = _payload(reviewed).get("implementer")
            evidence = _payload(completed).get("acceptance_receipts")
            contract = _payload(completed).get('operator_repair_contract')
            claim = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed' ORDER BY id DESC LIMIT 1",(repair.id,completed["run_id"])).fetchone()
            if not run or not implementer or run["profile"] == implementer or not evidence or not isinstance(contract,dict) or contract.get('original_task_id')!=tid or contract.get('owner')!='agent' or not claim or json.loads(claim["payload"] or "{}").get("source_status") != "review":
                _exception(conn,tid,"repair_acceptance_unproved",repair_task_id=repair.id)
                continue
            if _last(conn,tid,'operator_repair_verified'):
                continue  # This one repair cannot repeatedly renew original execution.
            original_deadline=_last(conn,tid,'operator_deadline')
            if original_deadline and now>=_payload(original_deadline).get('due_at',0):
                try:
                    original=kb.get_task(conn,tid)
                    reviewer=dispatch_owner(conn,cfg.get('review_assignee','reviewer'))
                    if reviewer==original.assignee:
                        reviewer=acceptance_owner(conn,cfg,original.assignee)
                    with kb.write_txn(conn):
                        conn.execute("UPDATE tasks SET max_runtime_seconds=MIN(COALESCE(max_runtime_seconds,?),?) WHERE id=? AND status='blocked' AND claim_lock IS NULL",(runtime,runtime,tid))
                    accepted,reason=kb.request_review(conn,tid,reviewer=reviewer,summary='The bounded agent repair passed native independent acceptance. Verify the original requested outcome against existing effects and repair evidence, and deliver a substantive result. Original execution time is exhausted: do not replay external actions or claim Harry receipt.',resume_verified_repair=True,with_reason=True)
                    if not accepted:
                        raise ValueError(reason or 'native original acceptance handoff refused')
                    with kb.write_txn(conn):
                        kb._append_event(conn,tid,'operator_repair_verified',{'repair_task_id':repair.id,'review_run_id':completed['run_id'],'disposition':'bounded_original_acceptance'})
                        kb._append_event(conn,tid,'operator_acceptance_recovery',{'owner':'agent','repair_task_id':repair.id,'reviewer':reviewer,'due_at':now+runtime,'acceptance':'unproved'})
                    actions.append({'task_id':tid,'verified_repair_acceptance':repair.id})
                except Exception as error:
                    _exception(conn,tid,'verified_repair_acceptance_handoff_failed',error=str(error)[:300])
                continue
            if kb.unblock_task(conn, tid):
                with kb.write_txn(conn):
                    kb._append_event(conn, tid, "operator_repair_verified",
                                     {"repair_task_id": repair.id, "review_run_id": completed["run_id"]})
                actions.append({"task_id": tid, "resumed_after_repair": repair.id})
        else:
            first_review=conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='review_requested' ORDER BY id LIMIT 1",(repair.id,)).fetchone()
            review_handoff=_last(conn,repair.id,"operator_agent_review_handoff")
            phase_due=(_payload(review_handoff)["due_at"] if review_handoff else first_review["created_at"]+runtime) if first_review else info["due_at"]
            if now < phase_due:
                continue
            if not _last(conn, tid, "operator_repair_overdue"):
                with kb.write_txn(conn):
                    kb._append_event(conn, tid, "operator_repair_overdue",
                                     {"repair_task_id": repair.id, "owner": "agent", "due_at": phase_due,"phase":"review" if first_review else "implementation"})
                actions.append({"task_id": tid, "exception": "repair_deadline_exceeded"})
            # A total repair deadline must prevent endless goal-mode attempts.
            # Native external block proves the worker tree gone before clearing
            # its claim; refused/unknown identities remain fenced and retry here.
            stopped = False
            if repair.status in ("ready","running"):
                stopped = kb.block_task(conn,repair.id,reason="Agent-owned repair deadline exhausted; original request remains fenced.",kind='capability')
            elif repair.status in ("review","todo","triage") and repair.claim_lock is None:
                with kb.write_txn(conn):
                    stopped = conn.execute("UPDATE tasks SET status='blocked',block_kind='capability' WHERE id=? AND status=? AND claim_lock IS NULL",(repair.id,repair.status)).rowcount > 0
            if stopped:
                with kb.write_txn(conn):
                    kb._append_event(conn,repair.id,"operator_repair_stopped",{"owner":"agent","due_at":phase_due,"original_task_id":tid})
                actions.append({"task_id":tid,"repair_stopped":repair.id})
    return actions


def reconcile(conn, *, board=None, settings=None, now=None):
    """A faulty operator row must never stop the existing reclaim/spawn loop."""
    from hermes_cli import kanban_db as kb
    try:
        return _reconcile(conn, board=board, settings=settings, now=now)
    except Exception as error:
        kb._log.exception("operator reconciliation failed; native reclaim/spawn continues")
        return [{"exception":"reconciliation_failed", "owner":"agent", "error":type(error).__name__}]
