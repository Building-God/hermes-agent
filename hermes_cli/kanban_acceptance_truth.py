"""Native acceptance facts and terminal fences, not a second execution plane."""
import json,re
from pathlib import Path


def _last(conn,tid,kind):
    return conn.execute('SELECT * FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1',(tid,kind)).fetchone()


def _data(row):
    try:return json.loads(row['payload'] or '{}') if row else {}
    except (ValueError,TypeError):return {}


def health_snapshot(conn,cfg,current_task=None):
    requests=[]
    for tid in list(cfg.get('cohort_task_ids',[]))[:16]:
        if tid==current_task:continue
        task=conn.execute('SELECT id,status,assignee,block_kind,result FROM tasks WHERE id=?',(tid,)).fetchone()
        if not task:continue
        complete=_last(conn,tid,'completed');review=_last(conn,tid,'review_requested')
        invalid=_last(conn,tid,'operator_acceptance_invalidated')
        proof=bool(complete and review and complete['id']>review['id'] and task['result'] and _data(complete).get('acceptance_receipts'))
        fault=bool((invalid and (not complete or invalid['id']>complete['id'])) or
                   (task['status'] in ('blocked','triage') and task['block_kind']!='needs_input') or
                   (task['status'] in ('done','archived') and not proof) or
                   (task['status'] in ('review','running') and _last(conn,tid,'operator_acceptance_recovery')))
        requests.append({'task_id':tid,'status':task['status'],'owner':task['assignee'],'acceptance_provenance':proof,
                         'unresolved_agent_fault':fault,'completed_event_id':complete['id'] if complete else None})
    platforms={}
    declaration=cfg.get('release_entrypoint') or {}
    if declaration.get('home'):
        path=Path(declaration['home'])/'gateway_state.json'
        try:platforms={name:value.get('state') for name,value in json.loads(path.read_text(encoding='utf-8')).get('platforms',{}).items()}
        except (OSError,ValueError,TypeError,AttributeError):platforms={'gateway':'unproved'}
    return {'requests':requests,'platforms':platforms,'human_result_receipt':'separate; not inferred','scope':'declared genuine request cohort and live platform state; no board-total health inference'}


def positive_health_claim(text):
    return bool(re.search(r'\bsystem\s+(?:is\s+)?(?:operational\s*(?:and|&)\s*)?healthy\b|\bno\s+silent\s+failures\b',str(text or ''),re.I))


def completion_failure(conn,tid,text,cfg):
    from hermes_cli.kanban_operator import agent_answer_claim_failure
    failure=agent_answer_claim_failure(conn,tid,text)
    if failure:return failure
    if not positive_health_claim(text):return None
    snapshot=health_snapshot(conn,cfg,current_task=tid)
    faults=[r['task_id'] for r in snapshot['requests'] if r['unresolved_agent_fault']]
    unavailable=[p for p,state in snapshot['platforms'].items() if state!='connected']
    if faults or unavailable or not snapshot['requests']:
        return ('Unproved overall health claim: independently report request exceptions and outcomes, not file existence or board totals. '
                'Unresolved requests: '+','.join(faults)+'. Unavailable/unproved platforms: '+','.join(unavailable)+'. '
                'Issue a factual result with owners and exact next actions; delivery and human receipt stay separate.')
    return None


def execution_claim_allowed(conn,tid,source_status):
    from hermes_cli.kanban_operator import policy
    if not policy(conn).get('enabled'):return True
    stop=conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind IN ('operator_request_stopped','operator_repair_stopped','operator_acceptance_stopped','operator_terminal_recovered') ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
    if not stop:return True
    if source_status=='review':
        resume=conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind IN ('operator_acceptance_recovery','operator_agent_review_handoff') ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
        return bool(resume and resume['id']>stop['id'])
    return False


def require_archive_acceptance(conn,tid):
    from hermes_cli.kanban_operator import policy,agent_answer_claim_failure
    cfg=policy(conn)
    if not cfg.get('enabled'):return
    row=conn.execute('SELECT status,result,created_by FROM tasks WHERE id=?',(tid,)).fetchone()
    origin=conn.execute('SELECT platform,chat_id FROM task_user_origins WHERE task_id=?',(tid,)).fetchone()
    if not row or (not origin and row['created_by']!='operator-repair'):return
    complete=_last(conn,tid,'completed');review=_last(conn,tid,'review_requested');invalid=_last(conn,tid,'operator_acceptance_invalidated')
    accepted=bool(row['status']=='done' and complete and review and complete['id']>review['id'] and
                  (not invalid or complete['id']>invalid['id']) and row['result'] and _data(complete).get('acceptance_receipts'))
    if accepted:
        run=conn.execute('SELECT profile FROM task_runs WHERE id=?',(complete['run_id'],)).fetchone()
        claim=conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed' ORDER BY id DESC LIMIT 1",(tid,complete['run_id'])).fetchone()
        accepted=bool(run and _data(review).get('implementer') and run['profile']!=_data(review)['implementer'] and claim and _data(claim).get('source_status')=='review')
    if accepted and agent_answer_claim_failure(conn,tid,row['result']):accepted=False
    declared=(cfg.get('agent_owned_faults') or {}).get(tid,{})
    if accepted and declared.get('completed_event_id')==complete['id'] and declared.get('source') and declared.get('reason'):accepted=False
    delivered=not origin
    if accepted and origin:
        for event in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='result_delivered'",(tid,)):
            p=_data(event)
            if p.get('event_id')==complete['id'] and p.get('platform')==origin['platform'] and p.get('chat_id')==origin['chat_id']:delivered=True
    if not accepted or not delivered:
        raise ValueError('Archival cannot bypass original acceptance and same-thread delivery. Retain the owned review or agent exception; correct and deliver the substantive result. Human result receipt is separate and never fabricated.')
