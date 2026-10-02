"""Native acceptance facts and terminal fences, not a second execution plane."""
import json,re,time
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
    if source_status=='ready':
        task=conn.execute('SELECT created_by FROM tasks WHERE id=?',(tid,)).fetchone()
        cfg=policy(conn)
        origin=conn.execute('SELECT 1 FROM task_user_origins WHERE task_id=?',(tid,)).fetchone()
        original_phase=_last(conn,tid,'operator_acceptance_recovery')
        original_changes=_last(conn,tid,'changes_requested')
        original_rework=_last(conn,tid,'operator_rework_due')
        if (origin and tid in cfg.get('cohort_task_ids',[]) and task and task['created_by']!='operator-repair'
            and original_phase and original_phase['id']>stop['id'] and original_changes and original_rework
            and original_rework['id']>original_phase['id'] and _data(original_rework).get('owner')=='agent'):
            native_recovery=conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind IN ('operator_rework_clock_recovered','operator_execution_gate_recovered') ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
            changed_after_phase=original_changes['id']>original_phase['id']
            recovered_changes=bool(native_recovery and native_recovery['id']>stop['id'] and _data(native_recovery).get('source_changes_event_id')==original_changes['id'])
            cycles=conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='changes_requested'",(tid,)).fetchone()[0]
            due=max(_data(original_phase).get('due_at',0),_data(original_rework).get('due_at',0))
            return bool((changed_after_phase or recovered_changes) and time.time()<due and cycles<int(cfg.get('max_review_cycles',3)))
        phase=_last(conn,tid,'operator_agent_review_handoff')
        changes=_last(conn,tid,'changes_requested')
        if task and task['created_by']=='operator-repair' and phase and phase['id']>stop['id'] and changes and changes['id']>phase['id']:
            from hermes_cli.kanban_operator import repair_contract
            contract,failure=repair_contract(conn,tid)
            cycles=conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='changes_requested' AND id>?",(tid,phase['id'])).fetchone()[0]
            due=_data(phase).get('due_at',0)
            rework=_last(conn,tid,'operator_rework_due')
            if rework and rework['id']>phase['id']:due=min(due,_data(rework).get('due_at',due))
            return bool(contract and not failure and time.time()<due and cycles<int(policy(conn).get('max_review_cycles',3)))
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


def native_state_report(conn,tid,cfg):
    """Answer declared status requests with independent native reads, not shell claims."""
    from hermes_cli.kanban_operator import repair_authority
    authority=repair_authority(conn,tid)
    original_id=authority['event']['task_id'] if authority else tid
    if original_id not in cfg.get('state_report_task_ids',[]) or original_id not in cfg.get('cohort_task_ids',[]):return None
    if not conn.execute('SELECT 1 FROM task_user_origins WHERE task_id=?',(original_id,)).fetchone():return None
    facts=health_snapshot(conn,cfg,current_task=original_id)
    lines=['Live evidence for the recent requests I audited:']
    for name in ('discord','api_server','whatsapp'):
        lines.append('- '+name+': '+str(facts['platforms'].get(name,'unproved'))+'.')
    for item in facts['requests']:
        origin=conn.execute('SELECT text FROM task_user_origins WHERE task_id=?',(item['task_id'],)).fetchone()
        label=' '.join((origin['text'] if origin else item['task_id']).split())[:120]
        evidence=('agent-owned failure remains unresolved' if item['unresolved_agent_fault'] else 'recorded state; functional outcome still needs its own evidence')
        lines.append('- '+label+' ['+item['task_id']+']: '+item['status']+', owner '+str(item['owner'])+'; '+evidence+'.')
    choices=[item['task_id'] for item in facts['requests'] if item['status']=='blocked' and conn.execute('SELECT block_kind FROM tasks WHERE id=?',(item['task_id'],)).fetchone()['block_kind']=='needs_input']
    lines.append('Waiting for Harry in this audited scope: '+(', '.join(choices) if choices else 'no recorded human-only choice; agent deployment and review remain agent-owned')+'.')
    lines.append('Older cards outside this audit remain unverified. Gateway connection and board activity do not establish capability parity. Human result receipt is separate and is not inferred from sending this answer.')
    return '\n'.join(lines),facts


def repair_review_failure(conn,tid,reason):
    """Reject the observed reviewer instruction that contradicts native ownership."""
    from hermes_cli.kanban_operator import policy,repair_authority
    if not policy(conn).get('enabled') or not repair_authority(conn,tid):return None
    text=str(reason or '')
    if re.search(r'needs_input',text,re.I) and re.search(r'block_kind|sql\s+update|update\s+tasks|still.{0,30}dependency',text,re.I):
        if re.search(r'(?:do not|must not|remove|reject|wrong|incorrect).{0,50}needs_input',text,re.I):return None
        return 'Review contradicts native repair authority: the original must retain its agent-owned dependency hold until independently accepted. Changing block_kind to needs_input is not the requested functional repair. Use native Kanban reads and actual outcome probes; do not demand SQL ownership mutation or Harry deployment.'
    return None
