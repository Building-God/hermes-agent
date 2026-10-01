"""Controlled failure drills using actual board APIs and isolated Hermes homes."""
import json
import asyncio
import pytest
from tools import kanban_tools as kanban_tools_surface
from hermes_cli import kanban_acceptance_truth as truth

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_operator as operator
from gateway.front_door_deadline import ensure_continuation, build_bounded_ack


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(dispatch, "_dispatch_profile_allowlist", lambda normalize: None)
    monkeypatch.setattr(dispatch, "_profile_exists_fn", lambda: lambda name: name in {"pilot","reviewer"})
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in {"pilot","reviewer"})
    from hermes_cli.config import load_config
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban":{"default_assignee":"pilot"}})
    conn = kb.connect()
    yield conn
    conn.close()


def entry():
    return dict(platform="discord", conversation_id="channel", thread_id="thread", user_id="harry",
                message_id="message", message="Produce the weekly briefing", idempotency_key="discord:channel:message")


def accepted_origin(board, monkeypatch, text="Independently verified result"):
    tid=kb.create_task(board,title="real outcome",assignee="pilot",user_origin={"platform":"discord","chat_id":"c","message_id":"audit","user_id":"harry","text":"Explain the result"})
    settings={"enabled":True,"cohort_task_ids":[tid],"acceptance_recheck_assignee":"pilot"}
    monkeypatch.setattr(operator,"policy",lambda conn:settings)
    worker=kb.claim_task(board,tid)
    assert kb.request_review(board,tid,reviewer="reviewer",expected_run_id=worker.current_run_id)
    reviewer=kb.claim_review_task(board,tid)
    assert kb.complete_task(board,tid,result=text,expected_run_id=reviewer.current_run_id,metadata={"acceptance_receipts":{"probe":"actual source and live result reproduced"}})
    return tid,settings


def test_global_health_claim_requires_request_outcomes_not_board_counts(board,monkeypatch):
    other=kb.create_task(board,title="failed request",assignee="pilot")
    assert kb.block_task(board,other,kind="capability",reason="agent-owned repair pending")
    settings={"enabled":True,"cohort_task_ids":[other]}
    assert truth.completion_failure(board,"current","System is OPERATIONAL & HEALTHY",settings)
    assert truth.completion_failure(board,"current","No silent failures",settings)
    assert truth.completion_failure(board,"current","One request remains blocked on an agent repair; human receipt is unobserved.",settings) is None
    snapshot=truth.health_snapshot(board,settings)
    assert snapshot['requests'][0]['unresolved_agent_fault']
    assert snapshot['human_result_receipt']=='separate; not inferred'


def test_archive_cannot_discard_failed_request_to_stop_churn(board,monkeypatch):
    tid=ensure_continuation(entry())
    monkeypatch.setattr(operator,"policy",lambda conn:{"enabled":True,"cohort_task_ids":[tid]})
    assert kb.block_task(board,tid,kind="capability",reason="agent review failed")
    with pytest.raises(ValueError,match="Archival cannot bypass"):
        kb.archive_task(board,tid,reason="Stop deadline churn")
    assert kb.get_task(board,tid).status=='blocked'


def test_archive_requires_same_origin_delivery_but_not_human_receipt(board,monkeypatch):
    tid,settings=accepted_origin(board,monkeypatch)
    complete=operator._last(board,tid,'completed')
    with pytest.raises(ValueError,match="same-thread delivery"):
        kb.archive_task(board,tid)
    with kb.write_txn(board):
        kb._append_event(board,tid,'result_delivered',{'event_id':complete['id'],'platform':'discord','chat_id':'wrong'})
    with pytest.raises(ValueError,match="same-thread delivery"):
        kb.archive_task(board,tid)
    with kb.write_txn(board):
        kb._append_event(board,tid,'result_delivered',{'event_id':complete['id'],'platform':'discord','chat_id':'c'})
    kb.archive_task(board,tid)
    assert kb.get_task(board,tid).status=='archived'
    assert operator._last(board,tid,'result_receipt') is None


def test_native_claim_fence_survives_explicit_queue_promotion(board,monkeypatch):
    tid=ensure_continuation(entry())
    monkeypatch.setattr(operator,"policy",lambda conn:{"enabled":True})
    with kb.write_txn(board):
        kb._append_event(board,tid,'operator_request_stopped',{'owner':'agent','due_at':1})
    assert kb.block_task(board,tid,kind="capability",reason="deadline exhausted")
    assert kb.unblock_task(board,tid)
    before=board.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',(tid,)).fetchone()[0]
    assert kb.claim_task(board,tid) is None
    assert board.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?',(tid,)).fetchone()[0]==before


def test_stopped_repair_cannot_be_claimed_after_auto_unblock(board,monkeypatch):
    tid=kb.create_task(board,title="bounded repair",assignee="pilot",created_by="operator-repair")
    monkeypatch.setattr(operator,"policy",lambda conn:{"enabled":True})
    with kb.write_txn(board):
        kb._append_event(board,tid,'operator_repair_stopped',{'owner':'agent','due_at':1})
    assert kb.block_task(board,tid,kind="capability",reason="repair deadline exhausted")
    assert kb.unblock_task(board,tid)
    assert kb.claim_task(board,tid) is None


def test_declared_bad_completion_gets_exact_once_corrective_review(board,monkeypatch):
    tid,settings=accepted_origin(board,monkeypatch)
    completed=operator._last(board,tid,'completed')
    settings['agent_owned_faults']={tid:{'completed_event_id':completed['id'],'source':'independent audit','reason':'Board totals were falsely presented as overall health'}}
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,tid).status=='review'
    due=operator._payload(operator._last(board,tid,'operator_acceptance_recovery'))['due_at']
    operator.reconcile(board,settings=settings)
    assert operator._payload(operator._last(board,tid,'operator_acceptance_recovery'))['due_at']==due
    reviewer=kb.claim_review_task(board,tid)
    assert kb.complete_task(board,tid,result="Qualified answer with concrete unresolved agent faults",expected_run_id=reviewer.current_run_id,metadata={'acceptance_receipts':{'audit':'reproduced live faults'}})
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,tid).status=='done'


def test_historical_invalidated_archive_gets_one_bounded_acceptance_phase(board,monkeypatch):
    tid,settings=accepted_origin(board,monkeypatch)
    completed=operator._last(board,tid,'completed')
    with kb.write_txn(board):
        kb._append_event(board,tid,'operator_acceptance_invalidated',{'source_completed_event_id':completed['id'],'reason':'false human confirmation'})
        board.execute("UPDATE tasks SET status='archived' WHERE id=?",(tid,))
        kb._append_event(board,tid,'archived',{'reason':'legacy bypass'})
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,tid).status=='review'
    recovery=operator._last(board,tid,'operator_acceptance_recovery')
    assert operator._last(board,tid,'operator_terminal_review_handoff')
    assert kb.claim_review_task(board,tid) is not None
    operator.reconcile(board,settings=settings)
    assert operator._last(board,tid,'operator_acceptance_recovery')['id']==recovery['id']
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_terminal_recovered'",(tid,)).fetchone()[0]==1


def test_audited_repair_authority_fault_has_fixed_review_not_execution_replay(board,monkeypatch):
    tid=kb.create_task(board,title='Original authorized outcome',assignee='pilot',user_origin={'platform':'discord','chat_id':'c','message_id':'repair-audit','user_id':'harry','text':'Repair the original outcome'})
    kb.claim_task(board,tid)
    dispatch._record_task_failure(board,tid,'controlled crash',outcome='crashed',failure_limit=1,release_claim=True,end_run=True)
    settings={'enabled':True,'cohort_task_ids':[tid],'acceptance_recheck_assignee':'reviewer','attempt_seconds':120}
    operator.reconcile(board,settings=settings)
    repair=operator._payload(operator._last(board,tid,'operator_repair_created'))['repair_task_id']
    assert kb.block_task(board,repair,kind='capability',reason='A profile routing decision is needed before agent deployment')
    hold=operator._last_hold(board,repair)
    settings['audited_repair_faults']={repair:{'event_id':hold['id'],'source':'independent exact audit','reason':'Deployment is already authorized; routing belongs to the agent'}}
    monkeypatch.setattr(operator,'policy',lambda conn:settings)
    with kb.write_txn(board):
        kb._append_event(board,repair,'operator_repair_stopped',{'due_at':1,'owner':'agent'})
        stopped=operator._last(board,repair,'operator_repair_stopped')['id']
    settings['audited_repair_faults'][repair]['event_id']=stopped
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,repair).status=='review'
    handoff=operator._last(board,repair,'operator_authority_review_handoff')
    assert operator._payload(handoff)['due_at']>hold['created_at']
    assert kb.claim_review_task(board,repair)
    operator.reconcile(board,settings=settings)
    assert operator._last(board,repair,'operator_authority_review_handoff')['id']==handoff['id']
    assert kb.get_task(board,tid).status=='blocked'
    assert kb.get_task(board,tid).block_kind=='dependency'


@pytest.mark.parametrize('stale',[False,True])
def test_audited_terminal_repair_fault_requires_latest_exact_event(board,monkeypatch,stale):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    assert kb.block_task(board,repair,kind='capability',reason='Worker judged stale candidate rather than authenticated outcome',expected_run_id=review.current_run_id)
    hold=operator._last_hold(board,repair)
    with kb.write_txn(board):
        kb._append_event(board,repair,'operator_repair_stopped',{'owner':'agent','due_at':1})
        stopped=operator._last(board,repair,'operator_repair_stopped')['id']
    settings.update(cohort_task_ids=[tid],audited_repair_faults={repair:{'event_id':hold['id'] if stale else stopped,'source':'controlled exact worker goal audit','reason':'Shared worker goal now follows immutable original request'}})
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,repair).status==('blocked' if stale else 'review'),operator._payload(operator._last(board,repair,'operator_exception'))
    operator.reconcile(board,settings=settings)
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_authority_review_handoff'",(repair,)).fetchone()[0]==(0 if stale else 1)
    assert kb.get_task(board,tid).status=='blocked'


def test_new_audited_phase_rework_ignores_old_due_but_keeps_new_deadline(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    assert kb.block_task(board,repair,kind='capability',reason='Stale worker goal caused timeout',expected_run_id=review.current_run_id)
    with kb.write_txn(board):
        kb._append_event(board,repair,'operator_rework_due',{'due_at':1,'owner':'agent'})
        kb._append_event(board,repair,'operator_repair_stopped',{'due_at':1,'owner':'agent'})
        stopped=operator._last(board,repair,'operator_repair_stopped')['id']
    settings.update(cohort_task_ids=[tid],audited_repair_faults={repair:{'event_id':stopped,'source':'exact independently reproduced worker fault','reason':'Corrected goal contract'}})
    operator.reconcile(board,settings=settings)
    worker=kb.claim_review_task(board,repair)
    assert worker,operator._payload(operator._last(board,repair,'operator_exception'))
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,repair).status=='running'
    ok,owner=kb.request_changes(board,repair,reason='Functional probe fails with concrete reproduction',expected_run_id=worker.current_run_id)
    assert ok and owner=='pilot'
    assert kb.claim_task(board,repair)
    phase=operator._last(board,repair,'operator_agent_review_handoff')
    monkeypatch.setattr(truth.time,'time',lambda:operator._payload(phase)['due_at']+1)
    assert not truth.execution_claim_allowed(board,repair,'ready')
    assert kb.get_task(board,tid).status=='blocked'
    assert kb.claim_task(board,tid) is None


def test_worker_goal_repairs_follow_authentic_request_and_owned_review_step(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch,request_text='Report what is currently broken')
    settings.update(cohort_task_ids=[tid],state_report_task_ids=[tid])
    goal=kb.task_goal_text(board,review)
    assert 'Report what is currently broken' in goal
    assert 'truthful negative status satisfies' in goal
    assert 'kanban_request_changes' in goal
    assert 'kanban_request_changes' not in kb.task_goal_text(board,review,for_completion=True)


def test_safe_release_authority_reaches_worker_context_only_for_declared_cohort(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    settings.update(cohort_task_ids=[tid],agent_action_authority={'safe_live_release':True,'source':'Controlled explicit human build authorization'})
    text=kb.build_worker_context(board,repair)
    assert 'Harry has authorized implementation, deployment and live reproduction' in text
    assert 'Your owned step is independent review' in text
    assert 'Do not edit the immutable serving tree' in text
    assert 'Do not relabel AGENT answers as HARRY' in text
    settings['cohort_task_ids']=[]
    assert 'Harry has authorized implementation, deployment and live reproduction' not in kb.task_goal_text(board,review)


@pytest.mark.parametrize('completion',[False,True])
def test_owned_reviewer_goal_does_not_require_review_of_review(board,monkeypatch,completion):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    goal=kb.task_goal_text(board,review,for_completion=completion)
    assert 'Your owned step is independent review' in goal
    assert 'Your own read-only probes and kanban_complete record independent acceptance' in goal
    assert 'No prior review, deployment rights or Harry receipt is required' in goal
    assert 'Independent native acceptance remains mandatory' not in goal
    text=kb.build_worker_context(board,repair)
    assert 'Request review from a different installed profile' not in text
    assert 'Never ask the implementer to self-certify' in text


def test_implementation_goal_hands_off_before_independent_acceptance(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    assert kb.request_changes(board,repair,reason='actual failed probe',expected_run_id=review.current_run_id)
    build=kb.claim_task(board,repair)
    goal=kb.task_goal_text(board,build)
    assert 'Independent acceptance happens after kanban_request_review' in goal
    assert 'Request review from a different installed profile' in kb.build_worker_context(board,repair)


@pytest.mark.parametrize('kind',[None,'dependency','transient','capability'])
def test_manual_owned_review_block_is_native_rework(board,monkeypatch,kind):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    settings['cohort_task_ids']=[tid]
    assert kb.block_task(board,repair,reason='Candidate requires agent deployment; orchestrator decision',kind=kind,expected_run_id=review.current_run_id)
    landed=kb.get_task(board,repair)
    assert landed.status=='ready'
    assert landed.assignee=='pilot'
    assert operator._last(board,repair,'blocked') is None
    event=operator._last(board,repair,'operator_failed_review_rework')
    assert operator._payload(event)['source']=='manual_block'
    assert kb.get_task(board,tid).status=='blocked'
    assert kb.get_task(board,tid).block_kind=='dependency'


def test_native_repair_context_does_not_inject_wrong_candidate_scope(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch,request_text='Repair my real conversation and queue headers')
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET title=?,body=? WHERE id=?',('Only check cycle flags','FORBIDDEN OLD SCOPE: headers are out of scope; ask Harry to deploy',repair))
    text=kb.build_worker_context(board,repair)
    assert 'Repair my real conversation and queue headers' in text
    assert 'FORBIDDEN OLD SCOPE' not in text
    assert 'Only check cycle flags' not in text.splitlines()[0]
    assert 'Candidate description retained as history' in text


def test_cohort_original_context_does_not_inject_wrong_candidate_scope(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch,request_text='Repair my real conversation and queue headers')
    settings['cohort_task_ids']=[tid]
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET title=?,body=? WHERE id=?',('Only count cards','FORBIDDEN OLD SCOPE: headers require a different job',tid))
    text=kb.build_worker_context(board,tid)
    assert 'Repair my real conversation and queue headers' in text
    assert 'FORBIDDEN OLD SCOPE' not in text
    assert 'Only count cards' not in text.splitlines()[0]


@pytest.mark.parametrize('case',['stale','outside_cohort','human_choice'])
def test_manual_review_rework_preserves_scope_and_run_ownership(board,monkeypatch,case):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    settings['cohort_task_ids']=[tid]
    if case=='stale':
        assert not kb.block_task(board,repair,reason='stale reviewer',expected_run_id=review.current_run_id+100)
        assert kb.get_task(board,repair).current_run_id==review.current_run_id
    elif case=='outside_cohort':
        settings['cohort_task_ids']=[]
        assert kb.block_task(board,repair,reason='legacy hold',expected_run_id=review.current_run_id)
        assert kb.get_task(board,repair).status=='blocked'
    else:
        with pytest.raises(ValueError,match='cannot ask Harry'):
            kb.block_task(board,repair,reason='choose for me',kind='needs_input',expected_run_id=review.current_run_id)
        assert kb.get_task(board,repair).current_run_id==review.current_run_id
    assert operator._last(board,repair,'operator_failed_review_rework') is None


@pytest.mark.parametrize('case',['native','unknown_owner','wrong_repair','human_hold'])
def test_exact_audited_candidate_can_follow_historical_native_restoration_only(board,monkeypatch,case):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    assert kb.block_task(board,repair,kind='capability',reason='Exact source defect independently reproduced',expected_run_id=review.current_run_id)
    fault=operator._last_hold(board,repair)
    with kb.write_txn(board):
        kb._append_event(board,tid,'operator_repair_hold_reconciled',{'owner':'agent' if case!='unknown_owner' else 'unknown','repair_task_id':repair if case!='wrong_repair' else 't_other','previous_status':'blocked','previous_block_kind':'capability'})
        if case=='human_hold':
            board.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?",(tid,))
            kb._append_event(board,tid,'blocked',{'kind':'needs_input','reason':'Which mutually exclusive recipient do you choose?','source_status':'ready'})
    assert operator.repair_contract(board,repair)[1]
    drift=operator._last(board,tid,'operator_repair_hold_reconciled')
    settings.update(cohort_task_ids=[tid],audited_repair_faults={repair:{'event_id':fault['id'],'source':'Controlled independent exact source audit','reason':'Corrected source; old native restoration retained as history'}})
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,repair).status==('review' if case=='native' else 'blocked')
    assert operator._last(board,tid,'operator_repair_hold_reconciled')['id']==drift['id']
    if case=='native':
        assert operator.repair_contract(board,repair)[1] is None
        assert kb.claim_review_task(board,repair)
        with kb.write_txn(board):kb._append_event(board,tid,'operator_repair_hold_reconciled',{'owner':'agent','repair_task_id':repair,'previous_block_kind':'needs_input'})
        assert operator.repair_contract(board,repair)[1]
    if case=='human_hold':
        operator.reconcile(board,settings=settings)
        assert kb.get_task(board,tid).block_kind=='needs_input'
        assert kb.get_task(board,repair).status=='blocked'


def test_stale_repair_audit_does_not_override_a_later_fault(board,monkeypatch):
    tid,repair,info,settings=historical_repair_review_hold(board)
    hold=operator._last_hold(board,repair)
    settings.update(cohort_task_ids=[tid],audited_repair_faults={repair:{'event_id':hold['id'],'source':'audit','reason':'old agent fault'}})
    assert kb.unblock_task(board,repair)
    assert kb.block_task(board,repair,kind='capability',reason='New independently observed credentials failure')
    monkeypatch.setattr(operator,'policy',lambda conn:settings)
    operator.reconcile(board,settings=settings)
    assert operator._last(board,repair,'operator_authority_review_handoff') is None
    assert kb.get_task(board,repair).status=='blocked'


def failed_origin_repair(board):
    tid=kb.create_task(board,title='Original failed outcome',assignee='pilot',user_origin={'platform':'discord','chat_id':'c','message_id':'edge-proof','user_id':'harry','text':'Fix the original requested outcome'})
    kb.claim_task(board,tid)
    dispatch._record_task_failure(board,tid,'controlled crash',outcome='crashed',failure_limit=1,release_claim=True,end_run=True)
    settings={'enabled':True,'cohort_task_ids':[tid]}
    operator.reconcile(board,settings=settings)
    repair=operator._payload(operator._last(board,tid,'operator_repair_created'))['repair_task_id']
    return tid,repair,settings


def test_repair_dependency_cannot_wait_for_its_failed_original(board,monkeypatch):
    tid,repair,settings=failed_origin_repair(board)
    kb.link_tasks(board,parent_id=tid,child_id=repair)
    unrelated=kb.create_task(board,title='Separate prerequisite',assignee='pilot')
    kb.link_tasks(board,parent_id=unrelated,child_id=repair)
    monkeypatch.setattr(operator,'policy',lambda conn:settings)
    operator.reconcile(board,settings=settings)
    links={tuple(r) for r in board.execute('SELECT parent_id,child_id FROM task_links')}
    assert (tid,repair) not in links
    assert (repair,tid) in links
    assert (unrelated,repair) in links
    assert kb.get_task(board,tid).status=='blocked'
    assert kb.get_task(board,tid).block_kind=='dependency'
    event=operator._last(board,tid,'operator_repair_dependency_corrected')
    operator.reconcile(board,settings=settings)
    assert operator._last(board,tid,'operator_repair_dependency_corrected')['id']==event['id']


def test_dependency_correction_rolls_back_when_another_path_would_cycle(board,monkeypatch):
    tid,repair,settings=failed_origin_repair(board)
    middle=kb.create_task(board,title='Other dependency path',assignee='pilot')
    kb.link_tasks(board,parent_id=tid,child_id=repair)
    kb.link_tasks(board,parent_id=tid,child_id=middle)
    kb.link_tasks(board,parent_id=middle,child_id=repair)
    monkeypatch.setattr(operator,'policy',lambda conn:settings)
    operator.reconcile(board,settings=settings)
    links={tuple(r) for r in board.execute('SELECT parent_id,child_id FROM task_links')}
    assert (tid,repair) in links and (repair,tid) not in links
    assert operator._payload(operator._last(board,tid,'operator_exception'))['reason']=='repair_prerequisite_correction_failed'


def test_terminal_recovery_has_new_actual_evidence_after_rejected_candidate(board,monkeypatch):
    tid,settings=accepted_origin(board,monkeypatch)
    completed=operator._last(board,tid,'completed')
    with kb.write_txn(board):
        kb._append_event(board,tid,'changes_requested',{'reason':'False acceptance; correct the claimed human confirmation'})
        kb._append_event(board,tid,'operator_acceptance_invalidated',{'source_completed_event_id':completed['id'],'reason':'Actual actor was AGENT'})
        board.execute("UPDATE tasks SET status='archived' WHERE id=?",(tid,))
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,tid).status=='review'
    receipt=operator._payload(operator._last(board,tid,'review_requested'))['acceptance_receipts']['native_terminal_correction']
    assert receipt['source_completed_event_id']==completed['id']
    assert 'unproved' in receipt['scope']
    assert operator._last(board,tid,'completed')['id']==completed['id']


def test_state_answer_is_native_verified_facts_not_unreproduced_shell_narration(board,monkeypatch):
    tid,settings=accepted_origin(board,monkeypatch)
    other=kb.create_task(board,title='Unresolved real work',assignee='pilot')
    assert kb.block_task(board,other,kind='capability',reason='Agent failure remains')
    settings['cohort_task_ids'].append(other)
    settings['state_report_task_ids']=[tid]
    assert kb.request_review(board,tid,reviewer='pilot',resume_audited_origin=True)
    review=kb.claim_review_task(board,tid)
    assert kb.complete_task(board,tid,result='System OPERATIONAL & HEALTHY; all problems fixed',expected_run_id=review.current_run_id)
    answer=kb.get_task(board,tid).result
    assert other in answer and 'blocked' in answer and 'agent-owned failure remains unresolved' in answer
    assert 'HEALTHY' not in answer
    assert 'Older cards outside this audit remain unverified' in answer
    complete=operator._payload(operator._last(board,tid,'completed'))
    facts=complete['acceptance_receipts']['native_live_state']['requests']
    assert any(item['task_id']==other and item['status']=='blocked' for item in facts)
    assert operator._last(board,tid,'operator_narrative_superseded')


def test_native_review_rejects_wrong_ownership_predicate_without_spending_rework(board,monkeypatch):
    tid,repair,settings=failed_origin_repair(board)
    monkeypatch.setattr(operator,'policy',lambda conn:settings)
    worker=kb.claim_task(board,repair)
    assert kb.request_review(board,repair,reviewer='reviewer',expected_run_id=worker.current_run_id)
    review=kb.claim_review_task(board,repair)
    ok,reason=kb.request_changes(board,repair,reason="block_kind is STILL dependency, not needs_input. Execute the SQL UPDATE on the live database.",expected_run_id=review.current_run_id)
    assert not ok and 'contradicts native repair authority' in reason
    assert kb.get_task(board,repair).status=='running'
    assert kb.get_task(board,tid).block_kind=='dependency'
    assert operator._last(board,repair,'changes_requested') is None
    assert operator._last(board,repair,'operator_review_contract_rejected')
    ok,reason=kb.request_changes(board,repair,reason='The old reviewer was wrong to require needs_input; the actual Discord outcome still fails reproduction.',expected_run_id=review.current_run_id)
    assert ok


def test_failed_corrective_acceptance_has_one_final_terminal_phase(board,monkeypatch):
    tid,settings=accepted_origin(board,monkeypatch)
    completed=operator._last(board,tid,'completed')
    with kb.write_txn(board):
        kb._append_event(board,tid,'operator_acceptance_invalidated',{'source_completed_event_id':completed['id'],'reason':'False acceptance'})
        kb._append_event(board,tid,'operator_acceptance_stopped',{'owner':'agent','due_at':1})
        board.execute("UPDATE tasks SET status='blocked',block_kind='capability' WHERE id=?",(tid,))
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,tid).status=='review'
    phase=operator._last(board,tid,'operator_acceptance_recovery')
    due=operator._payload(phase)['due_at']
    operator.reconcile(board,settings=settings,now=due)
    operator.reconcile(board,settings=settings,now=due+1)
    assert operator._last(board,tid,'operator_acceptance_recovery')['id']==phase['id']
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_terminal_review_handoff'",(tid,)).fetchone()[0]==1


def test_state_query_failure_repairs_and_delivers_original_without_inline_shell_or_harry(board,monkeypatch):
    tid,repair,settings=failed_origin_repair(board)
    settings['state_report_task_ids']=[tid]
    settings['acceptance_recheck_assignee']='reviewer'
    monkeypatch.setattr(operator,'policy',lambda conn:settings)
    child=kb.create_task(board,title='Agent sign-off child',assignee='reviewer')
    kb.link_tasks(board,parent_id=repair,child_id=child)
    claim=kb.claim_task(board,repair)
    assert kb.block_task(board,repair,reason='Goal judge says external sign-off on '+child+' is outside agent authority',expected_run_id=claim.current_run_id)
    with kb.write_txn(board):
        kb._append_event(board,tid,'operator_deadline',{'due_at':1,'owner':'agent'})
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,repair).status=='review'
    review=kb.claim_review_task(board,repair)
    assert kb.complete_task(board,repair,result='Inline shell is restricted; cannot verify',expected_run_id=review.current_run_id)
    assert operator._payload(operator._last(board,repair,'completed'))['acceptance_receipts']['native_live_state']
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,tid).status=='review'
    review=kb.claim_review_task(board,tid)
    assert kb.complete_task(board,tid,expected_run_id=review.current_run_id)
    assert 'Live evidence' in kb.get_task(board,tid).result
    assert 'receipt is separate' in kb.get_task(board,tid).result
    assert operator._last(board,tid,'result_receipt') is None


def test_corrective_review_keeps_implementation_owner_for_live_rework(board,monkeypatch):
    tid,repair,settings=failed_origin_repair(board)
    settings['acceptance_recheck_assignee']='other'
    monkeypatch.setattr(dispatch,'_profile_exists_fn',lambda:lambda name:name in {'pilot','reviewer','other'})
    monkeypatch.setattr('hermes_cli.profiles.profile_exists',lambda name:name in {'pilot','reviewer','other'})
    worker=kb.claim_task(board,repair)
    assert kb.request_review(board,repair,reviewer='reviewer',expected_run_id=worker.current_run_id)
    review=kb.claim_review_task(board,repair)
    child=kb.create_task(board,title='Sign-off descendant',assignee='other')
    kb.link_tasks(board,parent_id=repair,child_id=child)
    assert kb.block_task(board,repair,kind='dependency',reason='Awaiting '+child,expected_run_id=review.current_run_id)
    # Historical snapshot: the prior reviewer has exited; no physical worker is spawned in this drill.
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET claim_lock=NULL,claim_expires=NULL WHERE id=?',(repair,))
    monkeypatch.setattr(operator,'policy',lambda conn:settings)
    operator.reconcile(board,settings=settings)
    operator.reconcile(board,settings=settings)
    handoff=operator._payload(operator._last(board,repair,'review_requested'))
    assert handoff['implementer']=='pilot' and handoff['reviewer']=='other', (handoff,operator._payload(operator._last(board,repair,'operator_exception')))
    review=kb.claim_review_task(board,repair)
    ok,owner=kb.request_changes(board,repair,reason='The original functional result still needs a deployed fix',expected_run_id=review.current_run_id)
    assert ok and owner=='pilot'
    assert kb.get_task(board,repair).assignee=='pilot'


def test_corrective_repair_can_rework_within_fixed_phase_but_never_after_new_stop(board,monkeypatch):
    import time
    tid,repair,settings=failed_origin_repair(board)
    monkeypatch.setattr(operator,'policy',lambda conn:settings)
    worker=kb.claim_task(board,repair)
    assert kb.request_review(board,repair,reviewer='reviewer',expected_run_id=worker.current_run_id)
    with kb.write_txn(board):
        kb._append_event(board,repair,'operator_repair_stopped',{'due_at':1,'owner':'agent'})
        kb._append_event(board,repair,'operator_agent_review_handoff',{'due_at':time.time()+60,'owner':'agent','reviewer':'reviewer'})
    review=kb.claim_review_task(board,repair)
    ok,owner=kb.request_changes(board,repair,reason='The original live UI still has duplicate prompts',expected_run_id=review.current_run_id)
    assert ok and owner=='pilot'
    worker=kb.claim_task(board,repair)
    assert worker is not None
    assert kb.get_task(board,tid).status=='blocked'
    assert kb.block_task(board,repair,kind='capability',reason='Final phase exhausted',expected_run_id=worker.current_run_id)
    with kb.write_txn(board):
        kb._append_event(board,repair,'operator_repair_stopped',{'due_at':1,'owner':'agent'})
    assert kb.unblock_task(board,repair)
    assert kb.claim_task(board,repair) is None


def test_lost_original_route_restores_exact_reply_without_replaying_false_history(board,monkeypatch):
    tid,settings=accepted_origin(board,monkeypatch)
    completed=operator._last(board,tid,'completed')
    with kb.write_txn(board):
        kb._append_event(board,tid,'result_transport_sent',{'event_id':completed['id'],'platform':'discord','chat_id':'c','thread_id':'thread','message_id':'old-false-answer'})
        kb._append_event(board,tid,'operator_acceptance_invalidated',{'source_completed_event_id':completed['id'],'reason':'False human confirmation'})
        board.execute("UPDATE tasks SET status='archived' WHERE id=?",(tid,))
    operator.reconcile(board,settings=settings)
    sub=board.execute('SELECT * FROM kanban_notify_subs WHERE task_id=?',(tid,)).fetchone()
    assert sub and (sub['platform'],sub['chat_id'],sub['thread_id'],sub['user_id'])==('discord','c','thread','harry')
    assert json.loads(sub['delivery_metadata'])['reply_to_message_id']=='audit'
    invalid=operator._last(board,tid,'operator_acceptance_invalidated')
    assert sub['last_event_id']>=invalid['id']
    assert sub['last_artifact_event_id']>=invalid['id']
    restored=operator._last(board,tid,'operator_original_route_restored')
    operator.reconcile(board,settings=settings)
    assert operator._last(board,tid,'operator_original_route_restored')['id']==restored['id']
    assert operator._last(board,tid,'result_receipt') is None


def test_invalid_result_withdrawn_preserves_exact_evidence_and_does_not_clear_later_truth(board,monkeypatch):
    tid,settings=accepted_origin(board,monkeypatch,text='The old answer wrongly claimed human confirmation')
    completed=operator._last(board,tid,'completed')
    with kb.write_txn(board):
        kb._append_event(board,tid,'operator_acceptance_invalidated',{'source_completed_event_id':completed['id'],'reason':'False acceptance'})
        board.execute("UPDATE tasks SET status='archived' WHERE id=?",(tid,))
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,tid).result is None
    withdrawn=operator._payload(operator._last(board,tid,'operator_invalid_result_withdrawn'))
    assert withdrawn['previous_result']=='The old answer wrongly claimed human confirmation'
    assert withdrawn['source_completed_event_id']==completed['id']
    review=kb.claim_review_task(board,tid)
    assert kb.complete_task(board,tid,result='New independently reproduced answer; human confirmation is unobserved',expected_run_id=review.current_run_id,metadata={'acceptance_receipts':{'probe':'Actual current original outcome'}})
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,tid).result.startswith('New independently reproduced')


def test_deadline_creates_owned_runnable_task_and_exact_route_once(board):
    first = ensure_continuation(entry())
    assert ensure_continuation(entry()) == first
    assert kb.get_task(board, first).status == "ready"
    assert kb.get_task(board, first).assignee == "pilot"
    assert board.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    route = board.execute("SELECT * FROM kanban_notify_subs").fetchone()
    assert (route["chat_id"], route["thread_id"], route["user_id"]) == ("channel", "thread", "harry")
    assert json.loads(route["delivery_metadata"])["reply_to_message_id"] == "message"


def test_deadline_reuses_already_owned_request(board):
    original = kb.create_task(board, title="original", assignee="pilot",
                              user_origin={"platform":"discord", "chat_id":"channel", "message_id":"message",
                                           "user_id":"harry", "text":"Produce the weekly briefing"})
    assert ensure_continuation(entry()) == original


def test_missing_owner_never_promises_background_execution(board):
    bad = entry(); bad["user_id"] = ""
    with pytest.raises(ValueError):
        ensure_continuation(bad)
    assert "background and" not in build_bounded_ack(bad)
    assert board.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_automatic_failure_has_one_repair_owner_and_no_harry_question(board):
    tid = kb.create_task(board, title="failed work", assignee="pilot")
    task = kb.claim_task(board, tid)
    dispatch._record_task_failure(board, tid, "controlled crash", outcome="crashed", failure_limit=1,
                                  release_claim=True, end_run=True)
    settings = dict(enabled=True, attempt_seconds=120, request_seconds=600)
    operator.reconcile(board, settings=settings, now=1000)
    operator.reconcile(board, settings=settings, now=1001)
    repairs = board.execute("SELECT * FROM tasks WHERE created_by='operator-repair'").fetchall()
    assert len(repairs) == 1
    assert repairs[0]["assignee"] == "pilot" and repairs[0]["status"] == "ready"
    assert kb.get_task(board, tid).status == "blocked"
    assert kb.get_task(board, tid).block_kind == "dependency"


def test_unreviewed_repair_cannot_resume_parent(board):
    tid = kb.create_task(board, title="failed work", assignee="pilot")
    kb.claim_task(board, tid)
    dispatch._record_task_failure(board, tid, "controlled crash", outcome="crashed", failure_limit=1,
                                  release_claim=True, end_run=True)
    settings = dict(enabled=True, attempt_seconds=120)
    operator.reconcile(board, settings=settings)
    repair = board.execute("SELECT id FROM tasks WHERE created_by='operator-repair'").fetchone()[0]
    kb.complete_task(board, repair, result="asserted fixed")
    operator.reconcile(board, settings=settings)
    assert kb.get_task(board, tid).status == "blocked"


def test_total_repair_deadline_stops_new_attempts_without_harry_input(board):
    tid=kb.create_task(board,title="failed original",assignee="pilot")
    kb.claim_task(board,tid)
    dispatch._record_task_failure(board,tid,"controlled failure",outcome="crashed",failure_limit=1,release_claim=True,end_run=True)
    settings=dict(enabled=True,attempt_seconds=120)
    operator.reconcile(board,settings=settings)
    repair=operator._payload(operator._last(board,tid,"operator_repair_created"))["repair_task_id"]
    due=operator._payload(operator._last(board,tid,"operator_repair_created"))["due_at"]
    operator.reconcile(board,settings=settings,now=due)
    assert kb.get_task(board,repair).status == "blocked"
    assert kb.get_task(board,repair).block_kind == 'capability'
    assert kb.get_task(board,tid).status == "blocked"
    operator.reconcile(board,settings=settings,now=due+60)
    assert dispatch.dispatch_once(board,dry_run=True).spawned == []
    assert board.execute("SELECT COUNT(*) FROM tasks WHERE created_by='operator-repair'").fetchone()[0] == 1
    assert operator._last(board,repair,"operator_repair_stopped")


def test_expired_active_repair_keeps_claim_when_native_tree_stop_is_refused(board,monkeypatch):
    tid=kb.create_task(board,title="failed original",assignee="pilot")
    kb.claim_task(board,tid)
    dispatch._record_task_failure(board,tid,"controlled failure",outcome="crashed",failure_limit=1,release_claim=True,end_run=True)
    settings=dict(enabled=True,attempt_seconds=120)
    operator.reconcile(board,settings=settings)
    info=operator._payload(operator._last(board,tid,"operator_repair_created")); repair=info["repair_task_id"]
    active=kb.claim_task(board,repair)
    monkeypatch.setattr(kb,"_fence_running_release",lambda *a,**k:(False,{"identity_unverified":True}))
    operator.reconcile(board,settings=settings,now=info["due_at"])
    assert kb.get_task(board,repair).current_run_id == active.current_run_id
    assert kb.get_task(board,repair).claim_lock == active.claim_lock
    assert kb.get_task(board,tid).status == "blocked"
    assert operator._last(board,tid,"operator_repair_overdue")


def test_rejected_candidate_cannot_spin_through_same_review(board, monkeypatch):
    monkeypatch.setattr(operator, "policy", lambda *args: {"enabled": True})
    tid = kb.create_task(board, title="candidate", assignee="pilot")
    build = kb.claim_task(board, tid)
    assert kb.request_review(board, tid, summary="candidate", reviewer="reviewer", expected_run_id=build.current_run_id)
    review = kb.claim_review_task(board, tid)
    assert kb.request_changes(board, tid, reason="live effect missing", expected_run_id=review.current_run_id)[0]
    build = kb.claim_task(board, tid)
    ok, reason = kb.request_review(board, tid, summary="same work, new reassuring prose", reviewer="reviewer",
                                  expected_run_id=build.current_run_id, with_reason=True)
    assert not ok and "Rework required" in reason
    assert kb.get_task(board, tid).status == "running"
    assert kb.request_review(board, tid, summary="live effect reproduced", reviewer="reviewer",
                             expected_run_id=build.current_run_id,
                             metadata={"acceptance_receipts":{"live_sha":"new", "probe":"passed"}})


def test_attempt_budget_does_not_depend_on_heartbeat(board):
    tid = kb.create_task(board, title="stalled", assignee="pilot")
    kb.claim_task(board, tid)
    operator.reconcile(board, settings=dict(enabled=True, attempt_seconds=120))
    assert kb.get_task(board, tid).max_runtime_seconds == 120
    dispatch.heartbeat_worker(board, tid)
    assert kb.get_task(board, tid).max_runtime_seconds == 120


def test_reviewed_repair_resumes_exact_original_task(board,monkeypatch):
    tid = kb.create_task(board, title="failed work", assignee="pilot")
    kb.claim_task(board, tid)
    dispatch._record_task_failure(board, tid, "controlled crash", outcome="crashed", failure_limit=1,
                                  release_claim=True, end_run=True)
    settings = dict(enabled=True, attempt_seconds=120)
    operator.reconcile(board, settings=settings)
    repair = board.execute("SELECT id FROM tasks WHERE created_by='operator-repair'").fetchone()[0]
    build = kb.claim_task(board, repair)
    assert kb.request_review(board, repair, summary="fault reproduced and fixed", reviewer="reviewer", expected_run_id=build.current_run_id)
    review = kb.claim_review_task(board, repair)
    monkeypatch.setattr(operator,"policy",lambda conn: settings)
    with pytest.raises(ValueError,match="acceptance evidence required"):
        kb.complete_task(board, repair, result="reviewer says fixed", expected_run_id=review.current_run_id)
    assert kb.complete_task(board, repair, result="independent reproduction passed", expected_run_id=review.current_run_id,metadata={"acceptance_receipts":{"reproduction":"controlled failure reproduced then passed"}})
    assert kb.get_task(board, tid).status == "blocked"
    operator.reconcile(board, settings=settings)
    assert kb.get_task(board, tid).status == "ready"


def _owned_repair_candidate(board,monkeypatch,request_text=None):
    tid=kb.create_task(board,title='failed genuine work',assignee='pilot',user_origin={'platform':'discord','chat_id':'channel','message_id':'message','user_id':'harry','text':request_text or entry()['message']})
    kb.claim_task(board,tid)
    dispatch._record_task_failure(board,tid,'controlled crash',outcome='crashed',failure_limit=1,release_claim=True,end_run=True)
    settings={'enabled':True,'attempt_seconds':120}
    operator.reconcile(board,settings=settings)
    monkeypatch.setattr(operator,'policy',lambda conn:settings)
    repair=board.execute("SELECT id FROM tasks WHERE created_by='operator-repair'").fetchone()[0]
    build=kb.claim_task(board,repair)
    assert kb.request_review(board,repair,reviewer='reviewer',summary='candidate',expected_run_id=build.current_run_id)
    return tid,repair,kb.claim_review_task(board,repair),settings


def test_repair_wrong_ownership_rejects_native_completion_and_reworks(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?",(tid,))
    operator.reconcile(board,settings=settings)
    assert not kb.complete_task(board,repair,result='All tests pass: needs_input',expected_run_id=review.current_run_id,
                                metadata={'acceptance_receipts':['SQL query passes']})
    assert kb.get_task(board,repair).status=='ready'
    assert kb.get_task(board,repair).assignee=='pilot'
    assert kb.get_task(board,tid).block_kind=='dependency'
    assert operator._last(board,repair,'completed') is None
    change=operator._payload(operator._last(board,repair,'changes_requested'))
    assert 'native authority contract' in change['reason']
    lines=[];kb._ctx_header(lines,board,kb.get_task(board,repair))
    assert entry()['message'] in '\n'.join(lines)
    assert tid in '\n'.join(lines)


def test_repair_authority_rechecked_after_external_acceptance_collection(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    def race(*args):
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?",(tid,))
        return None
    monkeypatch.setattr('hermes_cli.kanban_pr_acceptance_store.prepare_acceptance',race)
    with pytest.raises(ValueError,match='native authority contract'):
        kb.complete_task(board,repair,result='passed',expected_run_id=review.current_run_id,
                         metadata={'acceptance_receipts':{'reproduction':'passed'}})
    assert kb.get_task(board,repair).status=='running'
    assert operator._last(board,repair,'completed') is None


def test_native_repair_contract_is_machine_created_and_resume_is_exact(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    assert kb.complete_task(board,repair,result='original transition reproduced',expected_run_id=review.current_run_id,
                            metadata={'acceptance_receipts':{'reproduction':'passed'},'operator_repair_contract':{'owner':'harry'}})
    receipt=operator._payload(operator._last(board,repair,'completed'))['operator_repair_contract']
    assert receipt['owner']=='agent'
    assert receipt['original_task_id']==tid
    assert receipt['origin_message_id']==entry()['message_id']
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,tid).status=='ready'


def test_agent_question_answer_cannot_become_harry_confirmation(board,monkeypatch):
    monkeypatch.setattr(operator,'policy',lambda conn:{'enabled':True})
    tid=kb.create_task(board,title='reasoning interface',assignee='pilot',user_origin={'platform':'discord','chat_id':'channel','message_id':'reason','user_id':'harry','text':'Make dash a reasoning partner'})
    build=kb.claim_task(board,tid)
    kb.add_comment(board,tid,author='default',body='AGENT[agent][q_28089d279474ab74]: It reasons like Discord now')
    kb.add_comment(board,tid,author='default',body='HARRY[q_unrelated]: That other item works')
    assert kb.request_review(board,tid,reviewer='reviewer',summary='candidate',expected_run_id=build.current_run_id)
    review=kb.claim_review_task(board,tid)
    assert not kb.complete_task(board,tid,result='Harry confirmed it on the live system',expected_run_id=review.current_run_id,
                                metadata={'acceptance_receipts':['Stored answer and185seconds means genuine user interaction']})
    assert kb.get_task(board,tid).status=='ready'
    assert 'False human acceptance' in operator._payload(operator._last(board,tid,'changes_requested'))['reason']
    assert operator._last(board,tid,'completed') is None
    assert operator._last(board,tid,'result_receipt') is None
    assert operator.agent_answer_claim_failure(board,tid,'Objective source and serving mode verified; human confirmation unobserved') is None
    assert operator.agent_answer_claim_failure(board,tid,"Harry's confirmation remains unobserved") is None
    assert operator.agent_answer_claim_failure(board,tid,'No authenticated Harry confirmation; source verified') is None
    kb.add_comment(board,tid,author='pilot',body='HARRY[q_28089d279474ab74]: It reasons like Discord now')
    assert operator.agent_answer_claim_failure(board,tid,'Harry confirmed this change')
    kb.add_comment(board,tid,author='default',body='HARRY[q_28089d279474ab74]: I tested that exact change')
    assert operator.agent_answer_claim_failure(board,tid,'Harry confirmed this change') is None
    assert operator._last(board,tid,'result_receipt') is None


def test_completed_false_human_acceptance_gets_one_bounded_corrective_review(board,monkeypatch):
    tid=kb.create_task(board,title='legacy wrong acceptance',assignee='reviewer',user_origin={'platform':'discord','chat_id':'c','message_id':'bad','user_id':'harry','text':'Make dash reason with me'})
    kb.claim_task(board,tid)
    assert kb.complete_task(board,tid,result='Harry confirmed it works')
    kb.add_comment(board,tid,author='default',body='AGENT[agent][q_old]: It reasons like Discord now')
    with kb.write_txn(board):
        kb._append_event(board,tid,'operator_acceptance_recovery',{'due_at':1,'owner':'agent'})
    settings={'enabled':True,'cohort_task_ids':[tid],'acceptance_recheck_assignee':'pilot','attempt_seconds':120}
    monkeypatch.setattr(operator,'policy',lambda conn:settings)
    operator.reconcile(board,settings=settings,now=100)
    assert kb.get_task(board,tid).status=='review'
    assert kb.get_task(board,tid).assignee=='pilot'
    assert operator._payload(operator._last(board,tid,'operator_acceptance_recovery'))['due_at']==220
    operator.reconcile(board,settings=settings,now=219)
    assert kb.get_task(board,tid).status=='review'
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_acceptance_invalidated'",(tid,)).fetchone()[0]==1
    operator.reconcile(board,settings=settings,now=220)
    assert kb.get_task(board,tid).status=='blocked'
    assert kb.get_task(board,tid).block_kind!='needs_input'
    assert operator._last(board,tid,'result_receipt') is None


def test_repair_cannot_wait_on_its_own_verification_descendant(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    assert kb.request_changes(board,repair,reason='Reproduce the real outcome',expected_run_id=review.current_run_id)[0]
    build=kb.claim_task(board,repair)
    child=kb.create_task(board,title='verification child',assignee='reviewer',parents=[repair])
    with pytest.raises(ValueError,match='verification descendant'):
        kb.block_task(board,repair,reason='Wait for '+child+' to verify',kind='dependency',expected_run_id=build.current_run_id)
    assert kb.get_task(board,repair).status=='running'
    assert kb.get_task(board,child).status=='todo'


def test_existing_circular_repair_hold_gets_one_bounded_review(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    assert kb.request_changes(board,repair,reason='Correct candidate',expected_run_id=review.current_run_id)[0]
    build=kb.claim_task(board,repair)
    kb.save_task_checkpoint(board,repair,expected_run_id=build.current_run_id,progress={'fault_reproduced':'native circular verification hold','next':'independent review existing changed evidence'})
    child=kb.create_task(board,title='verification child',assignee='reviewer',parents=[repair])
    monkeypatch.setattr(operator,'policy',lambda conn:{'enabled':False})
    assert kb.block_task(board,repair,reason='Wait for '+child+' to verify',kind='dependency',expected_run_id=build.current_run_id)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET block_kind='dependency' WHERE id=?",(repair,))
    settings['acceptance_recheck_assignee']='reviewer'
    monkeypatch.setattr(operator,'policy',lambda conn:settings)
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,repair).status=='review',operator._payload(operator._last(board,repair,'operator_exception'))
    assert kb.get_task(board,repair).assignee=='reviewer'
    first=operator._payload(operator._last(board,repair,'operator_agent_review_handoff'))
    operator.reconcile(board,settings=settings,now=first['due_at']-1)
    assert kb.get_task(board,repair).status=='review'
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_agent_dependency_review_handoff'",(repair,)).fetchone()[0]==1
    operator.reconcile(board,settings=settings,now=first['due_at'])
    assert kb.get_task(board,repair).status=='blocked'
    assert kb.get_task(board,tid).block_kind=='dependency'


def test_original_deadline_stops_idle_work_and_creates_one_owned_repair(board,monkeypatch):
    tid=kb.create_task(board,title='overdue real request',assignee='pilot',user_origin={'platform':'discord','chat_id':'c','message_id':'overdue','user_id':'harry','text':'Do this work'})
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET created_at=100 WHERE id=?',(tid,))
    settings={'enabled':True,'attempt_seconds':60,'request_seconds':120,'cohort_task_ids':[tid]}
    monkeypatch.setattr(operator,'policy',lambda conn:settings)
    operator.reconcile(board,settings=settings,now=220)
    assert kb.get_task(board,tid).status=='blocked'
    assert kb.get_task(board,tid).block_kind=='dependency'
    assert operator._payload(operator._last(board,tid,'operator_request_stopped'))['due_at']==220
    assert operator._last(board,tid,'operator_repair_created') is not None
    operator.reconcile(board,settings=settings,now=221)
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_repair_created'",(tid,)).fetchone()[0]==1


def test_verified_late_repair_hands_original_to_acceptance_without_replaying_work(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    assert kb.complete_task(board,repair,result='Transition repaired and independently reproduced',expected_run_id=review.current_run_id,metadata={'acceptance_receipts':{'probe':'reproduced'}})
    due=operator._payload(operator._last(board,tid,'operator_deadline'))['due_at']
    operator.reconcile(board,settings=settings,now=due+1)
    assert kb.get_task(board,tid).status=='review',operator._payload(operator._last(board,tid,'operator_exception'))
    assert operator._payload(operator._last(board,tid,'operator_repair_verified'))['disposition']=='bounded_original_acceptance'
    phase=operator._payload(operator._last(board,tid,'operator_acceptance_recovery'))
    operator.reconcile(board,settings=settings,now=due+2)
    assert kb.get_task(board,tid).status=='review'
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_repair_verified'",(tid,)).fetchone()[0]==1
    assert phase['due_at']==due+121
    monkeypatch.setattr(kb.time,'time',lambda:due+2)
    original_review=kb.claim_review_task(board,tid)
    assert kb.request_changes(board,tid,reason='Original outcome not yet proved',expected_run_id=original_review.current_run_id)[0]
    operator.reconcile(board,settings=settings,now=due+3)
    assert kb.get_task(board,tid).status=='ready'
    assert operator._payload(operator._last(board,tid,'operator_deadline'))['due_at']==due
    operator.reconcile(board,settings=settings,now=phase['due_at']+1)
    assert kb.get_task(board,tid).status=='blocked'
    assert kb.get_task(board,tid).block_kind!='needs_input'


def test_verified_repair_is_new_evidence_after_original_review_rejection(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    prior=operator.candidate_fingerprint(board,tid,None)
    with kb.write_txn(board):
        kb._append_event(board,tid,'review_requested',{'implementer':'pilot','reviewer':'reviewer','candidate_sha256':prior})
        kb._append_event(board,tid,'changes_requested',{'reason':'Actual probe unavailable; changed evidence required'})
    assert kb.complete_task(board,repair,result='Independent probe now reproduced',expected_run_id=review.current_run_id,metadata={'acceptance_receipts':{'probe':'actual new reproduction'}})
    completed=operator._last(board,repair,'completed')
    due=operator._payload(operator._last(board,tid,'operator_deadline'))['due_at']
    operator.reconcile(board,settings=settings,now=due+1)
    assert kb.get_task(board,tid).status=='review',operator._payload(operator._last(board,tid,'operator_exception'))
    evidence=operator._payload(operator._last(board,tid,'review_requested'))['acceptance_receipts']['native_verified_repair']
    assert evidence['completed_event_id']==completed['id']
    assert evidence['repair_task_id']==repair
    assert evidence['acceptance_receipts']['probe']=='actual new reproduction'
    operator.reconcile(board,settings=settings,now=due+2)
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_repair_verified'",(tid,)).fetchone()[0]==1


@pytest.mark.parametrize('native_guard',[True,False])
def test_false_kernel_review_block_reworks_once_only_with_exact_native_guard(board,monkeypatch,native_guard):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    if native_guard:
        with kb.write_txn(board):
            board.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?",(tid,))
        operator.reconcile(board,settings=settings)
        assert kb.get_task(board,tid).block_kind=='dependency'
    assert kb.block_task(board,repair,kind='dependency',reason='Kernel transaction isolation fault: SQL modifications automatically roll back',expected_run_id=review.current_run_id)
    operator.reconcile(board,settings=settings)
    if not native_guard:
        assert kb.get_task(board,repair).status=='blocked'
        assert operator._last(board,repair,'operator_invalid_review_rework') is None
        return
    assert kb.get_task(board,repair).status=='ready'
    assert kb.get_task(board,repair).assignee=='pilot'
    phase=operator._payload(operator._last(board,repair,'operator_invalid_review_rework'))
    assert phase['native_guard_event_id']==operator._last(board,tid,'operator_repair_hold_reconciled')['id']
    worker=kb.claim_task(board,repair)
    assert worker is not None
    assert kb.block_task(board,repair,kind='capability',reason='Recovery failed',expected_run_id=worker.current_run_id)
    operator.reconcile(board,settings=settings,now=phase['due_at']+1)
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_invalid_review_rework'",(repair,)).fetchone()[0]==1
    assert kb.get_task(board,repair).status=='blocked'
    assert kb.claim_task(board,repair) is None


def test_goal_judge_receives_original_repair_authority_and_structured_receipts(board,monkeypatch):
    tools=kanban_tools_surface
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    task=kb.get_task(board,repair)
    from dataclasses import replace
    task=replace(task,goal_mode=True,body='Stale reviewer says change block_kind to needs_input')
    captured={}
    def judge(**kwargs):
        captured.update(kwargs)
        return 'done','actual receipt accepted',None,None,False
    monkeypatch.setattr(tools,'_goal_judge_available',lambda:True)
    monkeypatch.setattr(tools,'judge_goal',judge)
    tools._goal_gate('kanban_complete',task,repair,'Probe reproduced',conn=board,metadata={'acceptance_receipts':{'probe':'actual source receipt'}})
    assert operator.repair_authority(board,repair)['origin']['text'] in captured['goal']
    assert 'Stale reviewer' not in captured['goal']
    assert 'actual source receipt' in captured['last_response']
    def blocked(**kwargs):return 'blocked','tool metadata misunderstood',None,None,False
    monkeypatch.setattr(tools,'judge_goal',blocked)
    with pytest.raises(tools._Reject,match='Do not hand deployment'):
        tools._goal_gate('kanban_complete',task,repair,'Candidate',conn=board)


@pytest.mark.parametrize('cap',[1,2])
def test_distinct_audited_fault_can_correct_tool_contract_with_finite_total_cap(board,monkeypatch,cap):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    monkeypatch.setattr(dispatch,'_profile_exists_fn',lambda:lambda name:name in {'pilot','reviewer','other'})
    monkeypatch.setattr('hermes_cli.profiles.profile_exists',lambda name:name in {'pilot','reviewer','other'})
    settings.update(cohort_task_ids=[tid],max_audited_repair_faults=cap,acceptance_recheck_assignee='other')
    with kb.write_txn(board):
        first=kb._append_event(board,repair,'operator_authority_review_handoff',{'audit_event_id':1,'due_at':1,'owner':'agent'})
    # Historical parked review predates rollout of the native manual-block
    # rework guard; retain that legacy failure to exercise lifetime audit caps.
    settings['cohort_task_ids']=[]
    assert kb.block_task(board,repair,kind='capability',reason='Goal judge failed to receive structured receipt metadata',expected_run_id=review.current_run_id)
    settings['cohort_task_ids']=[tid]
    hold=operator._last_hold(board,repair)
    settings['audited_repair_faults']={repair:{'event_id':hold['id'],'source':'independent source/actual failure audit','reason':'Tool schema and handler accept metadata; corrected judge now receives its actual structured receipts'}}
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,repair).status==('review' if cap==2 else 'blocked'),operator._payload(operator._last(board,repair,'operator_exception'))
    phases=board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_authority_review_handoff'",(repair,)).fetchone()[0]
    assert phases==cap
    operator.reconcile(board,settings=settings)
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_authority_review_handoff'",(repair,)).fetchone()[0]==phases


@pytest.mark.parametrize('case',['factual','functional','harry_prerequisite','claimed','unrelated_parent'])
def test_factual_repair_final_review_supersedes_only_its_extra_internal_signoff(board,monkeypatch,case):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch,request_text=('What is actually running and is anything broken?' if case!='functional' else 'Repair the functional router'))
    settings.update(cohort_task_ids=[tid])
    if case!='functional':settings['state_report_task_ids']=[tid]
    child=kb.create_task(board,title='Independent verification sign-off: audit factual status',created_by='pilot',assignee='pilot',parents=[repair],user_origin=({'platform':'discord','chat_id':'c','message_id':'separate-choice','user_id':'harry','text':'My separate prerequisite'} if case=='harry_prerequisite' else None))
    kb.link_tasks(board,child,tid)
    unrelated=None
    if case=='unrelated_parent':
        unrelated=kb.create_task(board,title='Independent external prerequisite',assignee='pilot',user_origin={'platform':'discord','chat_id':'c','message_id':'unrelated-choice','user_id':'harry','text':'Separate prerequisite'})
        kb.link_tasks(board,unrelated,tid)
    assert kb.complete_task(board,repair,result='Status facts independently reproduced',expected_run_id=review.current_run_id,metadata={'acceptance_receipts':{'probe':'actual status read'}})
    if case=='claimed':assert kb.claim_task(board,child)
    due=operator._payload(operator._last(board,tid,'operator_deadline'))['due_at']
    actions=operator.reconcile(board,settings=settings,now=due+1)
    child_edge=board.execute('SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?',(child,tid)).fetchone()
    assert bool(child_edge)==(case in ('functional','harry_prerequisite','claimed')),{'actions':actions,'exception':operator._payload(operator._last(board,tid,'operator_exception')),'repair_status':kb.get_task(board,repair).status,'receipt_keys':list(operator._payload(operator._last(board,repair,'completed')).get('acceptance_receipts',{})),'implementer':operator._payload(operator._last(board,repair,'review_requested')).get('implementer'),'child_created_by':kb.get_task(board,child).created_by}
    assert kb.get_task(board,child).status not in ('done','archived')
    assert operator._last(board,child,'completed') is None
    assert board.execute('SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?',(repair,child)).fetchone()
    if case=='factual':
        assert kb.get_task(board,tid).status=='review',operator._payload(operator._last(board,tid,'operator_exception'))
        worker=kb.claim_review_task(board,tid)
        assert worker and kb.complete_task(board,tid,result='System healthy',expected_run_id=worker.current_run_id)
        assert 'System healthy' not in kb.get_task(board,tid).result
    else:
        assert kb.get_task(board,tid).status=='blocked'
        assert kb.claim_task(board,tid) is None
    if unrelated:assert board.execute('SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?',(unrelated,tid)).fetchone()


def test_factual_original_goal_judge_sees_native_current_facts_not_candidate_health(board,monkeypatch):
    tools=kanban_tools_surface
    tid=kb.create_task(board,title='Stale positive health criteria',body='Everything must be healthy',assignee='pilot',goal_mode=True,user_origin={'platform':'discord','chat_id':'c','message_id':'status-goal','user_id':'harry','text':'What is actually running and is anything broken?'})
    settings={'enabled':True,'cohort_task_ids':[tid],'state_report_task_ids':[tid]}
    monkeypatch.setattr(operator,'policy',lambda conn:settings)
    monkeypatch.setattr(tools,'_goal_judge_available',lambda:True)
    captured={}
    def judge(**kwargs):captured.update(kwargs);return 'done','truthful status',None,None,False
    monkeypatch.setattr(tools,'judge_goal',judge)
    tools._goal_gate('kanban_complete',kb.get_task(board,tid),tid,'System healthy',conn=board)
    assert 'What is actually running' in captured['goal']
    assert 'truthful negative status satisfies' in captured['goal']
    assert 'Stale positive health criteria' not in captured['goal']
    assert 'System healthy' not in captured['last_response']
    assert 'Native live-state facts' in captured['last_response']


def test_exception_records_changed_cause_but_not_identical_retries(board):
    tid=kb.create_task(board,title='Observed handoff failure')
    operator._exception(board,tid,'handoff_failed',error='unchanged evidence')
    first=operator._last(board,tid,'operator_exception')
    operator._exception(board,tid,'handoff_failed',error='unchanged evidence')
    assert operator._last(board,tid,'operator_exception')['id']==first['id']
    operator._exception(board,tid,'handoff_failed',error='separate prerequisite unresolved')
    second=operator._last(board,tid,'operator_exception')
    assert second['id']>first['id']
    assert operator._payload(second)['error']=='separate prerequisite unresolved'
    operator._exception(board,tid,'handoff_failed',error='separate prerequisite unresolved')
    assert operator._last(board,tid,'operator_exception')['id']==second['id']
    with kb.write_txn(board):board.execute('UPDATE task_events SET payload=? WHERE id=?',('legacy malformed evidence',first['id']))
    operator._exception(board,tid,'handoff_failed',error='separate prerequisite unresolved')
    assert operator._last(board,tid,'operator_exception')['id']==second['id']


def test_unverified_repair_cannot_open_original_acceptance_phase(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    ok,reason=kb.request_review(board,tid,reviewer='reviewer',resume_verified_repair=True,with_reason=True)
    assert not ok
    assert 'independently verified native repair' in reason
    assert kb.get_task(board,tid).status=='blocked'


def test_formal_agent_dependency_resumes_without_harry(board):
    parent = kb.create_task(board, title="release", assignee="pilot")
    tid = kb.create_task(board, title="prove live result", assignee="reviewer", parents=[parent])
    kb.block_task(board, tid, reason="wait for release", kind="dependency")
    operator.reconcile(board, settings={"enabled":True})
    assert kb.get_task(board, tid).status == "todo"
    kb.complete_task(board, parent, result="verified live release")
    operator.reconcile(board, settings={"enabled":True})
    assert kb.get_task(board, tid).status == "ready"


def test_policy_follows_bound_board_not_worker_profile(board,tmp_path,monkeypatch):
    a = tmp_path / "a"; b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a/"board.json").write_text(json.dumps({"operator":{"enabled":True,"progress_seconds":90}}))
    (b/"board.json").write_text(json.dumps({"operator":{"enabled":False}}))
    ca=kb.connect(a/"kanban.db"); cb=kb.connect(b/"kanban.db")
    try:
        assert operator.policy(ca)["progress_seconds"] == 90
        assert operator.policy(cb)["enabled"] is False
        assert operator.policy(ca)["enabled"] is True
    finally:
        ca.close(); cb.close()


def test_unavailable_repair_owner_does_not_abort_other_dispatch_work(board,monkeypatch):
    tid=kb.create_task(board,title="fault",assignee="pilot")
    kb.claim_task(board,tid)
    dispatch._record_task_failure(board,tid,"crash",outcome="crashed",failure_limit=1,release_claim=True,end_run=True)
    monkeypatch.setattr(dispatch,"_profile_exists_fn",lambda: lambda name: False)
    operator.reconcile(board,settings={"enabled":True,"repair_assignee":"missing"})
    assert kb.get_task(board,tid).status == "blocked"
    assert operator._payload(operator._last(board,tid,"operator_exception"))["reason"] == "repair_owner_unavailable"


def test_intake_recovers_after_transient_creation_failure(board,monkeypatch):
    from gateway.front_door_deadline import journal_request,recover_unowned_intake
    from hermes_constants import get_hermes_home
    item=entry()
    journal_request(get_hermes_home(),item)
    recovered=recover_unowned_intake(get_hermes_home())
    assert recovered[0]["card_id"]
    assert recover_unowned_intake(get_hermes_home()) == []
    assert kb.get_task(board,recovered[0]["card_id"]) is not None


def test_continuation_fence_prevents_overlap_until_original_worker_stops(board):
    import threading
    from gateway.front_door_deadline import settle_foreground_fence
    from hermes_constants import get_hermes_home
    item=entry();item["foreground_fenced"]=True
    tid=ensure_continuation(item)
    assert kb.get_task(board,tid).status == "blocked"
    kb.recompute_ready(board)
    assert kb.claim_task(board,tid) is None
    stopped=threading.Event();stopped.set()
    settle_foreground_fence(item,get_hermes_home(),stopped)
    assert kb.get_task(board,tid).status == "ready"
    assert kb.claim_task(board,tid) is not None


def test_review_cycle_cap_and_due_rework_have_repair_owner(board,monkeypatch):
    monkeypatch.setattr(operator,"policy",lambda *a: {"enabled":True,"max_review_cycles":2,"rework_seconds":60})
    tid=kb.create_task(board,title="rejected result",assignee="pilot")
    for index in range(2):
        build=kb.claim_task(board,tid)
        assert kb.request_review(board,tid,summary="candidate",reviewer="reviewer",expected_run_id=build.current_run_id,
                                 metadata={"acceptance_receipts":{"candidate":index}})
        review=kb.claim_review_task(board,tid)
        assert kb.request_changes(board,tid,reason="independent reproduction fails",expected_run_id=review.current_run_id)
    assert kb.get_task(board,tid).status == "blocked"
    operator.reconcile(board,settings={"enabled":True,"repair_assignee":"pilot"})
    assert operator._last(board,tid,"operator_repair_created") is not None


def test_new_policy_does_not_retroactively_budget_old_worker(board):
    import time
    tid=kb.create_task(board,title="legacy attempt",assignee="pilot")
    kb.claim_task(board,tid)
    operator.reconcile(board,settings={"enabled":True,"activation_at":int(time.time())+10})
    assert kb.get_task(board,tid).max_runtime_seconds is None


def test_rollout_does_not_emit_exceptions_for_uncarried_historical_requests(board):
    tid=kb.create_task(board,title="old",assignee="pilot",user_origin={"platform":"discord","chat_id":"c","message_id":"old","user_id":"harry","text":"old request"})
    board.execute("UPDATE tasks SET created_at=1 WHERE id=?",(tid,));board.commit()
    operator.reconcile(board,settings={"enabled":True,"activation_at":100},now=10000)
    assert operator._last(board,tid,"operator_deadline") is None
    operator.reconcile(board,settings={"enabled":True,"activation_at":100,"cohort_task_ids":[tid]},now=10000)
    assert operator._last(board,tid,"operator_exception")


def test_missing_internal_dependency_never_becomes_harry_input(board,monkeypatch):
    monkeypatch.setattr(operator,"policy",lambda *a: {"enabled":True})
    parent=kb.create_task(board,title="deployment",assignee="pilot")
    tid=kb.create_task(board,title="verify",assignee="reviewer")
    run=kb.claim_task(board,tid)
    assert kb.block_task(board,tid,reason="Wait for "+parent,kind="dependency",expected_run_id=run.current_run_id)
    assert kb.get_task(board,tid).block_kind == "dependency"
    kb.recompute_ready(board)
    assert kb.get_task(board,tid).status == "blocked"
    operator.reconcile(board,settings={"enabled":True})
    assert kb.parent_ids(board,tid) == [parent]
    assert kb.get_task(board,tid).status == "todo"
    kb.complete_task(board,parent,result="live release reproduced")
    operator.reconcile(board,settings={"enabled":True})
    kb.recompute_ready(board)
    assert kb.get_task(board,tid).status == "ready"


def test_dependency_cycle_gets_bounded_agent_repair_not_harry_question(board,monkeypatch):
    monkeypatch.setattr(operator,"policy",lambda *a: {"enabled":True})
    tid=kb.create_task(board,title="original",assignee="pilot")
    verification=kb.create_task(board,title="verification",assignee="reviewer",parents=[tid])
    run=kb.claim_task(board,tid)
    assert kb.block_task(board,tid,reason="Wait for "+verification,kind="dependency",expected_run_id=run.current_run_id)
    operator.reconcile(board,settings={"enabled":True,"repair_assignee":"pilot"})
    assert kb.get_task(board,tid).block_kind == "dependency"
    assert operator._last(board,tid,"operator_repair_created")
    assert board.execute("SELECT COUNT(*) FROM tasks WHERE created_by='operator-repair'").fetchone()[0] == 1
    operator.reconcile(board,settings={"enabled":True,"repair_assignee":"pilot"})
    assert board.execute("SELECT COUNT(*) FROM tasks WHERE created_by='operator-repair'").fetchone()[0] == 1


def test_explicit_harry_only_choice_is_preserved(board,monkeypatch):
    monkeypatch.setattr(operator,"policy",lambda *a: {"enabled":True})
    tid=kb.create_task(board,title="human-only preference",assignee="pilot")
    run=kb.claim_task(board,tid)
    kb.block_task(board,tid,reason="Which of these mutually exclusive choices do you want?",kind="needs_input",expected_run_id=run.current_run_id)
    operator.reconcile(board,settings={"enabled":True})
    assert kb.get_task(board,tid).block_kind == "needs_input"
    assert operator._last(board,tid,"operator_repair_created") is None


def test_native_restart_uses_sealed_launcher_and_refuses_changed_controller(tmp_path):
    import hashlib
    from hermes_cli.gateway_windows import _selected_release_launcher
    runtime=tmp_path/"runtime";runtime.mkdir()
    launcher=runtime/"controller.py";launcher.write_text("verified controller")
    pointer=runtime/"active-release.json";pointer.write_text(json.dumps({"launcher":str(launcher),"launcher_sha256":hashlib.sha256(launcher.read_bytes()).hexdigest()}))
    assert _selected_release_launcher(tmp_path,"python.exe") == ["python.exe",str(launcher)]
    launcher.write_text("changed code")
    with pytest.raises(ValueError,match="launcher changed"):
        _selected_release_launcher(tmp_path,"python.exe")


def test_original_request_cannot_self_certify_and_keeps_canonical_result(board,monkeypatch):
    monkeypatch.setattr(operator,"policy",lambda *a: {"enabled":True})
    tid=kb.create_task(board,title="real request",assignee="pilot",user_origin={"platform":"discord","chat_id":"c","message_id":"m","user_id":"harry","text":"Deliver the requested result"})
    build=kb.claim_task(board,tid)
    with pytest.raises(ValueError,match="Independent review required"):
        kb.complete_task(board,tid,summary="I say it works",expected_run_id=build.current_run_id)
    assert kb.get_task(board,tid).status == "running"
    assert kb.request_review(board,tid,summary="Candidate ready",reviewer="reviewer",expected_run_id=build.current_run_id)
    review=kb.claim_review_task(board,tid)
    with pytest.raises(ValueError,match="Independent acceptance evidence required"):
        kb.complete_task(board,tid,summary="Reassuring summary only",expected_run_id=review.current_run_id)
    assert kb.complete_task(board,tid,summary="Independent outcome reproduced",expected_run_id=review.current_run_id,
                            metadata={"acceptance_receipts":{"controlled_reproduction":"passed"}})
    assert kb.get_task(board,tid).result == "Independent outcome reproduced"


def test_original_default_priority_beats_internal_backlog_without_overriding_explicit_priority(board,monkeypatch):
    # Real dispatch ordering with deterministic host capacity; unrelated live boards
    # and this machine's memory pressure are outside this isolated queue drill.
    monkeypatch.setattr(dispatch,"count_running_tasks_other_boards",lambda board: 0)
    monkeypatch.setattr(dispatch,"_memory_pressure_level",lambda: "normal")
    internal=kb.create_task(board,title="internal backlog",assignee="pilot")
    original=kb.create_task(board,title="original request",assignee="pilot",user_origin={"platform":"discord","chat_id":"c","message_id":"priority","user_id":"harry","text":"Do my work"})
    explicit=kb.create_task(board,title="explicit priority",assignee="pilot",priority=5,user_origin={"platform":"discord","chat_id":"c","message_id":"explicit","user_id":"harry","text":"Do this later"})
    operator.reconcile(board,settings={"enabled":True})
    assert kb.get_task(board,original).priority > kb.get_task(board,internal).priority
    assert kb.get_task(board,explicit).priority == 5
    picked=dispatch.dispatch_once(board,dry_run=True,max_spawn=1,max_in_progress=1)
    assert picked.spawned and picked.spawned[0][0] == original, picked


def test_new_attempt_cap_is_visible_before_claim_not_changed_under_worker(board):
    tid=kb.create_task(board,title="new attempt",assignee="pilot",max_runtime_seconds=5400)
    operator.reconcile(board,settings={"enabled":True,"attempt_seconds":900})
    assert kb.get_task(board,tid).max_runtime_seconds == 900
    run=kb.claim_task(board,tid)
    assert board.execute("SELECT max_runtime_seconds FROM task_runs WHERE id=?",(run.current_run_id,)).fetchone()[0] == 900


def test_unknown_foreground_shutdown_retains_fence(board):
    from gateway.front_door_deadline import settle_foreground_fence
    from hermes_constants import get_hermes_home
    item=entry();item["foreground_fenced"]=True
    tid=ensure_continuation(item)
    settle_foreground_fence(item,get_hermes_home(),None)
    assert kb.get_task(board,tid).status == "blocked"
    assert operator._payload(operator._last(board,tid,"operator_exception"))["reason"] == "foreground_shutdown_unconfirmed"


def test_agent_repair_cannot_turn_reviewer_approval_into_harry_input(board,monkeypatch):
    repair=kb.create_task(board,title="agent repair",assignee="pilot",created_by="operator-repair")
    monkeypatch.setattr(operator,"policy",lambda conn:{"enabled":True})
    with pytest.raises(ValueError,match="Agent-owned repair cannot ask Harry"):
        kb.block_task(board,repair,reason="Reviewer must approve",kind="needs_input")
    assert kb.get_task(board,repair).status == "ready"


def historical_repair_review_hold(board):
    from pathlib import Path
    tid=kb.create_task(board,title="original request",assignee="pilot")
    kb.claim_task(board,tid)
    dispatch._record_task_failure(board,tid,"crash",outcome="crashed",failure_limit=1,release_claim=True,end_run=True)
    settings={"enabled":True,"attempt_seconds":120}
    operator.reconcile(board,settings=settings)
    info=operator._payload(operator._last(board,tid,"operator_repair_created"));repair=info["repair_task_id"]
    claim=kb.claim_task(board,repair)
    workspace=Path(board.execute("PRAGMA database_list").fetchone()[2]).parent/"workspaces"/repair
    workspace.mkdir(parents=True)
    (workspace/"REPAIR_EVIDENCE.md").write_text("Candidate evidence, requires a different profile's reproduction.")
    (workspace/"VERIFICATION_OUTPUT.txt").write_text("Implementer's controlled probe output.")
    board.execute("UPDATE tasks SET workspace_path=? WHERE id=?",(str(workspace),repair));board.commit()
    assert kb.block_task(board,repair,reason="Reviewer should read REPAIR_EVIDENCE.md and VERIFICATION_OUTPUT.txt then approve.",kind="needs_input",expected_run_id=claim.current_run_id)
    return tid,repair,info,settings


def test_historical_review_hold_routes_candidate_to_owned_review_without_accepting_it(board,monkeypatch):
    tid,repair,info,settings=historical_repair_review_hold(board)
    # Reproduce the real worker's unjournaled relabeling of the original hold.
    board.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?",(tid,));board.commit()
    monkeypatch.setattr(operator,"policy",lambda conn:settings)
    operator.reconcile(board,settings=settings,now=info["due_at"]+1)
    assert kb.get_task(board,repair).status == "review",operator._payload(operator._last(board,repair,"operator_exception"))
    assert kb.get_task(board,repair).assignee == "reviewer"
    assert kb.get_task(board,tid).block_kind == "dependency"
    assert board.execute("SELECT COUNT(*) FROM task_attachments WHERE task_id=?",(repair,)).fetchone()[0] == 2
    review=kb.claim_review_task(board,repair)
    with pytest.raises(ValueError,match="acceptance evidence required"):
        kb.complete_task(board,repair,result="Report claims independence",expected_run_id=review.current_run_id)
    assert not kb.complete_task(board,repair,result="Different reviewer reproduced the outcome",expected_run_id=review.current_run_id,metadata={"acceptance_receipts":{"reproduction":"actual controlled probe"}})
    assert kb.get_task(board,repair).assignee=='pilot'
    assert operator._last(board,repair,'changes_requested') is not None
    assert kb.get_task(board,tid).status=='blocked'


def test_repair_review_has_fixed_separate_deadline_and_no_duplicate_handoff(board,monkeypatch):
    tid,repair,info,settings=historical_repair_review_hold(board)
    monkeypatch.setattr(operator,"policy",lambda conn:settings)
    started=info["due_at"]+1
    operator.reconcile(board,settings=settings,now=started)
    handoff=operator._payload(operator._last(board,repair,"operator_agent_review_handoff"))
    assert handoff.get("due_at") == started+120,operator._payload(operator._last(board,repair,"operator_exception"))
    operator.reconcile(board,settings=settings,now=started+119)
    assert kb.get_task(board,repair).status == "review"
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_agent_review_handoff'",(repair,)).fetchone()[0] == 1
    operator.reconcile(board,settings=settings,now=started+120)
    assert kb.get_task(board,repair).status == "blocked"
    assert kb.get_task(board,tid).status == "blocked"


def test_repair_review_recovery_cannot_reclassify_a_genuine_harry_choice(board,monkeypatch):
    tid=kb.create_task(board,title="Choose report audience",assignee="pilot",user_origin={"platform":"discord","chat_id":"c","message_id":"choice","user_id":"harry","text":"Prepare a report"})
    assert kb.block_task(board,tid,reason="Harry must choose the recipient",kind="needs_input")
    monkeypatch.setattr(operator,"policy",lambda conn:{"enabled":True})
    accepted,reason=kb.request_review(board,tid,reviewer="reviewer",resume_agent_repair=True,with_reason=True)
    assert not accepted and "operator repair" in reason
    assert kb.get_task(board,tid).status == "blocked"
    assert kb.get_task(board,tid).block_kind == "needs_input"


def test_legacy_genuine_completion_gets_one_independent_result_review(board,monkeypatch):
    tid=kb.create_task(board,title="legacy answer",assignee="pilot",user_origin={"platform":"discord","chat_id":"c","message_id":"legacy","user_id":"harry","text":"Explain my interface"})
    assert kb.complete_task(board,tid,summary="Claimed done but no delivered answer")
    assert kb.get_task(board,tid).result is None
    settings={"enabled":True,"cohort_task_ids":[tid],"attempt_seconds":120}
    monkeypatch.setattr(operator,"policy",lambda conn:settings)
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,tid).status == "review"
    assert kb.get_task(board,tid).assignee == "reviewer"
    operator.reconcile(board,settings=settings)
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_acceptance_recovery'",(tid,)).fetchone()[0] == 1
    review=kb.claim_review_task(board,tid)
    assert kb.complete_task(board,tid,summary="Substantive independently checked answer",expected_run_id=review.current_run_id,metadata={"acceptance_receipts":{"source_check":"reproduced original interface behaviour"}})
    assert kb.get_task(board,tid).result == "Substantive independently checked answer"
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,tid).status == "done"


def test_audited_agent_fault_is_exact_event_scoped_and_expires_without_harry_question(board,monkeypatch):
    tid=kb.create_task(board,title="deploy completed work",assignee="pilot",user_origin={"platform":"discord","chat_id":"c","message_id":"deployment","user_id":"harry","text":"Fix the duplicate queue"})
    assert kb.block_task(board,tid,reason="Reviewer/Harry must deploy",kind="needs_input")
    blocked=operator._last(board,tid,"blocked")
    settings={"enabled":True,"cohort_task_ids":[tid],"attempt_seconds":120,"agent_owned_faults":{tid:{"blocked_event_id":blocked["id"],"source":"confirmed operator audit","reason":"Deployment is already authorized; subjective confirmation remains unobserved."}}}
    monkeypatch.setattr(operator,"policy",lambda conn:settings)
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,tid).status == "review"
    due=operator._payload(operator._last(board,tid,"operator_acceptance_recovery"))["due_at"]
    operator.reconcile(board,settings=settings,now=due)
    assert kb.get_task(board,tid).status == "blocked"
    assert kb.get_task(board,tid).block_kind != "needs_input"
    assert operator._last(board,tid,"operator_repair_created")


def test_stale_audit_cannot_override_a_new_human_only_choice(board,monkeypatch):
    tid=kb.create_task(board,title="original",assignee="pilot",user_origin={"platform":"discord","chat_id":"c","message_id":"stale","user_id":"harry","text":"Prepare a report"})
    assert kb.block_task(board,tid,reason="An old deployment request",kind="needs_input")
    old=operator._last(board,tid,"blocked")
    assert kb.unblock_task(board,tid)
    assert kb.block_task(board,tid,reason="Harry must now choose the recipient",kind="needs_input")
    before=kb.get_task(board,tid)
    assert operator._last_hold(board,tid)["id"] > old["id"]
    settings={"enabled":True,"cohort_task_ids":[tid],"agent_owned_faults":{tid:{"blocked_event_id":old["id"],"source":"audit","reason":"old agent deployment fault"}}}
    monkeypatch.setattr(operator,"policy",lambda conn:settings)
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,tid).status == before.status
    assert kb.get_task(board,tid).block_kind == "needs_input"
    assert operator._last(board,tid,"operator_acceptance_recovery") is None


@pytest.mark.parametrize('case',['reviewer','stale','implementer','outside_cohort','human_hold'])
def test_actual_worker_goal_callback_returns_failed_review_to_implementer(board,monkeypatch,case):
    import contextlib,sys,types
    from hermes_cli import cli_single_query as worker
    from hermes_cli import goals
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    settings['cohort_task_ids']=[] if case=='outside_cohort' else [tid]
    if case=='human_hold':
        with kb.write_txn(board):board.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?",(tid,))
    active=review
    if case=='implementer':
        assert kb.request_changes(board,repair,reason='Actual functional probe fails',expected_run_id=review.current_run_id)[0]
        active=kb.claim_task(board,repair)
    monkeypatch.setenv('HERMES_KANBAN_TASK',repair)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID',str(active.current_run_id+1 if case=='stale' else active.current_run_id))
    monkeypatch.setitem(sys.modules,'cli',types.SimpleNamespace(_int_or=lambda v,d:int(v),_sync_cli_session_id_from_agent=lambda c:None))
    @contextlib.contextmanager
    def connected():yield board
    monkeypatch.setattr('hermes_cli.kanban_db_connect.connect_closing',connected)
    monkeypatch.setattr(goals,'run_kanban_goal_loop',lambda **kw:kw['block_fn']('Goal-mode judge ruled the goal unachievable: code review finished but deployment and live functional probe remain pending'))
    worker._run_kanban_goal_loop_q(types.SimpleNamespace(),first_response='Actual review cannot accept undeployed work')
    task=kb.get_task(board,repair)
    assert task.status==('ready' if case=='reviewer' else 'running' if case=='stale' else 'blocked')
    if case=='reviewer':
        assert task.assignee=='pilot'
        assert operator._last(board,repair,'operator_failed_review_rework')
        assert kb.claim_task(board,repair)
    assert kb.get_task(board,tid).status=='blocked'
    assert task.block_kind!='needs_input'


def test_intake_recovery_reconciles_concurrent_foreground_stop_receipt(board,monkeypatch):
    import gateway.front_door_deadline as fd
    from hermes_constants import get_hermes_home
    item=entry();item["foreground_fenced"]=True
    fd.journal_request(get_hermes_home(),item)
    real=fd.ensure_continuation
    def create_with_racing_stop(row,**kwargs):
        tid=real(row,**kwargs)
        stopped=dict(item,foreground_stopped=True,foreground_fenced=False,status="foreground_stopped")
        with fd.journal_path(get_hermes_home()).open("a",encoding="utf-8") as handle:
            handle.write(json.dumps(stopped)+"\n")
        return tid
    monkeypatch.setattr(fd,"ensure_continuation",create_with_racing_stop)
    recovered=fd.recover_unowned_intake(get_hermes_home())
    assert kb.get_task(board,recovered[0]["card_id"]).status == "ready"
    assert fd.recover_unowned_intake(get_hermes_home()) == []


from tests.plugins.test_a2a_phase23 import _make_live_adapter, _post_json, _send_body, _post_sse_authenticated

class TestInteractiveOperatorIngress:
    @pytest.mark.parametrize('stream',[False,True])
    @pytest.mark.parametrize('shared_alias',[False,True])
    def test_authenticated_interactive_peer_outlives_agent_loop_limit(self,monkeypatch,stream,shared_alias):
        monkeypatch.delenv('A2A_BEARER_TOKEN',raising=False)
        if shared_alias:monkeypatch.setenv('A2A_BEARER_TOKEN','fixture-interactive')
        monkeypatch.setenv('A2A_PEER_TOKENS','jarvis-interactive:fixture-interactive,worker:fixture-worker')
        monkeypatch.setenv('A2A_MAX_PINGPONG_TURNS','2')
        adapter,base=_make_live_adapter(monkeypatch,extra={'interactive_peers':['jarvis-interactive']})
        async def run():
            assert await adapter.connect()
            try:
                for i in range(3 if shared_alias else 6):
                    body=_send_body('Controlled interactive fixture turn '+str(i),ctx='fixture-interactive-context')
                    if stream:
                        # Same authenticated peer through the SSE handler.
                        body['method']='SendStreamingMessage'
                    response=await asyncio.to_thread(_post_json,base+'/',body,{'Authorization':'Bearer fixture-interactive'}) if not stream else await asyncio.to_thread(_post_sse_authenticated,base+'/',body,'fixture-interactive')
                    expected='TASK_STATE_REJECTED' if shared_alias and i==2 else 'TASK_STATE_COMPLETED'
                    if stream:
                        assert any(x.get('statusUpdate',{}).get('status',{}).get('state')==expected for x in response)
                    else:
                        assert response['result']['status']['state']==expected
                states=[]
                for i in range(3):
                    body=_send_body('agent fixture',ctx='fixture-agent-context')
                    body['params']['message']['metadata']={'interactive':True,'human':True,'peer':'jarvis-interactive'}
                    response=await asyncio.to_thread(_post_json,base+'/',body,{'Authorization':'Bearer fixture-worker'})
                    states.append(response['result']['status']['state'])
                assert states==['TASK_STATE_COMPLETED','TASK_STATE_COMPLETED','TASK_STATE_REJECTED']
            finally:
                await adapter.disconnect()
        asyncio.run(run())



@pytest.mark.parametrize('credential',['distinct','shared','expired'])
def test_operator_identity_requires_distinct_authenticated_interactive_credential(monkeypatch,credential):
    shared_alias=credential=='shared'
    expired=credential=='expired'
    monkeypatch.delenv('A2A_BEARER_TOKEN',raising=False)
    if shared_alias:monkeypatch.setenv('A2A_BEARER_TOKEN','fixture-ui-token')
    monkeypatch.setenv('A2A_PEER_TOKENS','fixture-ui:fixture-ui-token')
    events=[]
    def reply(event):
        events.append(event)
        return 'Controlled fixture response'
    adapter,base=_make_live_adapter(monkeypatch,reply_fn=reply,extra={'interactive_peers':['fixture-ui'],'peer_identities':{'fixture-ui':{'user':'fixture-human','user_name':'Fixture Operator','frame':'operator'}}})
    if expired:adapter._interactive_expiries['fixture-ui']=1
    async def run():
        assert await adapter.connect()
        try:
            body=_send_body('Controlled fixture reasoning request',ctx='fixture-operator-context')
            body['params']['message']['metadata']={'user':'forged-person','frame':'operator'}
            result=await asyncio.to_thread(_post_json,base+'/',body,{'Authorization':'Bearer fixture-ui-token'})
            assert result['result']['status']['state']=='TASK_STATE_COMPLETED'
            assert len(events)==1
            assert (events[0].source.user_id=='fixture-human') is (not shared_alias and not expired)
            assert (events[0].text=='Controlled fixture reasoning request') is (not shared_alias and not expired)
        finally:
            await adapter.disconnect()
    asyncio.run(run())


def test_owned_task_show_exposes_original_goal_before_candidate_history(board,monkeypatch):
    from tools import kanban_tools
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch,request_text='Restore my actual conversation')
    settings['cohort_task_ids']=[tid]
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET title=?,body=? WHERE id=?',('Check only cycles','WRONG CANDIDATE INSTRUCTION: ask Harry to edit SQL',repair))
    from contextlib import contextmanager
    @contextmanager
    def local_board(*args):yield kb,board
    monkeypatch.setattr(kanban_tools,'_board',local_board)
    result=json.loads(kanban_tools._handle_show({'task_id':repair}))
    assert 'Restore my actual conversation' in result['task']['body']
    assert 'WRONG CANDIDATE' not in result['task']['body']
    assert result['candidate_history']['body']=='WRONG CANDIDATE INSTRUCTION: ask Harry to edit SQL'
    assert 'historical evidence' in result['history_authority']
    assert 'independent review' in result['task']['body']


def test_owned_goal_accepts_independently_reproduced_existing_work(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    goal=kb.task_goal_text(board,review)
    assert 'Existing work from earlier runs' in goal
    assert 'not who authored the change' in goal
    assert 'metadata.acceptance_receipts' in goal


@pytest.mark.parametrize('declared',[True,False])
def test_dead_owned_review_protocol_failure_returns_native_rework(board,monkeypatch,declared):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    settings['cohort_task_ids']=[tid] if declared else []
    from hermes_cli import kanban_db_dispatch as native_dispatch
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET worker_pid=?,worker_started_at=?,claim_lock=? WHERE id=?',(987654,1,kb._host_prefix()+'controlled',repair))
        board.execute('UPDATE task_runs SET started_at=1 WHERE id=?',(review.current_run_id,))
    monkeypatch.setattr(native_dispatch,'_worker_alive',lambda *args:False)
    monkeypatch.setattr(native_dispatch,'_classify_dead_worker',lambda *args,**kw:native_dispatch._DeadWorker('clean_exit',0,'Controlled missing terminal outcome','protocol_violation',{'protocol_violation':True},protocol_violation=True))
    sweep=native_dispatch._reclaim_dead_workers(board)
    current=kb.get_task(board,repair)
    if not declared:
        assert current.status=='review'
        assert operator._last(board,repair,'operator_failed_review_rework') is None
        assert native_dispatch._account_crashes(board,sweep.crash_details)==[repair]
        assert kb.get_task(board,repair).status=='blocked'
        return
    assert current.status=='ready' and current.assignee=='pilot'
    assert current.current_run_id is None and current.worker_pid is None
    event=operator._last(board,repair,'operator_failed_review_rework')
    assert operator._payload(event)['source']=='worker_protocol'
    assert operator._last(board,repair,'protocol_violation')['run_id']==review.current_run_id
    assert native_dispatch._account_crashes(board,sweep.crash_details)==[]
    assert operator._last(board,repair,'gave_up') is None
    assert kb.get_task(board,tid).block_kind=='dependency'

@pytest.mark.parametrize('mode',['exact','stale','old_deadline_stop','human','outside','atomic_failure'])
def test_audited_original_correction_is_exact_finite_and_preserves_deadline(board,monkeypatch,mode):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch,request_text='Fix the actual fixture queue outcome')
    settings['cohort_task_ids']=[tid]
    assert kb.complete_task(board,repair,result='Controlled independent repair probe',expected_run_id=review.current_run_id,metadata={'acceptance_receipts':{'probe':'controlled receipt'}})
    due=operator._payload(operator._last(board,tid,'operator_deadline'))['due_at']
    monkeypatch.setattr(kb.time,'time',lambda:due+1)
    operator.reconcile(board,settings=settings,now=due+1)
    original_review=kb.claim_review_task(board,tid)
    assert kb.complete_task(board,tid,result='Controlled old result later independently disproved',expected_run_id=original_review.current_run_id,metadata={'acceptance_receipts':{'probe':'controlled old structural proof'}})
    completed=operator._last(board,tid,'completed')
    settings['agent_owned_faults']={tid:{'completed_event_id':completed['id'],'source':'controlled independent semantic audit','reason':'Actual fixture outcome failed'}}
    operator.reconcile(board,settings=settings,now=due+2)
    # Exhaust both existing native correction paths before auditing a NEW
    # framework failure. A stale audit must not suppress ordinary recovery.
    for _ in range(3):
        phase=operator._payload(operator._last(board,tid,'operator_acceptance_recovery'))
        cutoff=phase['due_at']+1
        monkeypatch.setattr(kb.time,'time',lambda:cutoff)
        operator.reconcile(board,settings=settings,now=cutoff)
        assert kb.get_task(board,tid).status=='blocked'
        operator.reconcile(board,settings=settings,now=cutoff+1)
        if kb.get_task(board,tid).status=='blocked':break
    assert kb.get_task(board,tid).status=='blocked'
    # Controlled injection of the observed OLD framework transition: the
    # expired original deadline killed an otherwise owned correction.
    with kb.write_txn(board):
        kb._append_event(board,tid,'operator_request_stopped',{'owner':'agent','due_at':due,'fixture':'old framework deadline stop'})
        kb._append_event(board,tid,'gave_up',{'owner':'agent','error':'Original request deadline exhausted','operator_rework':True,'sticky':True})
    hold=operator._last_hold(board,tid)
    stop=operator._last(board,tid,'operator_request_stopped')
    settings['agent_owned_faults'][tid].update({'blocked_event_id':hold['id']-(1 if mode=='stale' else 0),'deadline_stop_event_id':stop['id']-(1 if mode=='old_deadline_stop' else 0),'retry_acceptance':True,'source':'controlled framework correction','reason':'Old framework killed corrective transition'})
    if mode=='human':
        with kb.write_txn(board):board.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?",(tid,))
    if mode=='outside':settings['cohort_task_ids']=[]
    reviews_before=board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='review_requested'",(tid,)).fetchone()[0]
    owner_before=kb.get_task(board,tid).assignee
    if mode=='atomic_failure':
        native_append=kb._append_event
        def fail_phase(conn,task_id,kind,*args,**kwargs):
            if kind=='operator_audited_acceptance_recovery':raise RuntimeError('Controlled phase record failure')
            return native_append(conn,task_id,kind,*args,**kwargs)
        monkeypatch.setattr(kb,'_append_event',fail_phase)
    operator.reconcile(board,settings=settings,now=cutoff+2)
    assert operator._payload(operator._last(board,tid,'operator_deadline'))['due_at']==due
    audit=operator._last(board,tid,'operator_audited_acceptance_recovery')
    if mode!='exact':
        assert audit is None
        assert kb.get_task(board,tid).status=='blocked'
        if mode=='human':assert kb.get_task(board,tid).block_kind=='needs_input'
        if mode=='atomic_failure':
            assert kb.get_task(board,tid).assignee==owner_before
            assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='review_requested'",(tid,)).fetchone()[0]==reviews_before
        return
    assert audit is not None, json.dumps({'status':kb.get_task(board,tid).status,'kind':kb.get_task(board,tid).block_kind,'result':bool(kb.get_task(board,tid).result),'hold':dict(operator._last_hold(board,tid)),'exception':operator._payload(operator._last(board,tid,'operator_exception'))})
    assert kb.get_task(board,tid).status=='review'
    assert operator._payload(audit)['source_fault_event_id']==stop['id']
    new_due=operator._payload(audit)['due_at']
    assert new_due==cutoff+122
    operator.reconcile(board,settings=settings,now=new_due+1)
    assert kb.get_task(board,tid).status=='blocked'
    settings['agent_owned_faults'][tid]['blocked_event_id']=operator._last_hold(board,tid)['id']
    operator.reconcile(board,settings=settings,now=new_due+2)
    assert kb.get_task(board,tid).status=='blocked'
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='operator_audited_acceptance_recovery'",(tid,)).fetchone()[0]==1


@pytest.mark.parametrize('kind',['protocol_violation','gave_up','dependency_wait','block_loop_detected'])
def test_audit_intake_and_native_handoff_share_actual_worker_failure_types(board,monkeypatch,kind):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    settings['cohort_task_ids']=[tid]
    dispatch._record_task_failure(board,repair,'Controlled old worker missing terminal outcome',outcome='crashed',failure_limit=1,release_claim=True,end_run=True)
    with kb.write_txn(board):
        kb._append_event(board,repair,kind,{'owner':'agent','reason':'Controlled old worker terminal failure','protocol_violation':True})
    event=operator._last(board,repair,kind)
    assert operator.audited_repair_latest_fault_id(board,repair)==event['id']
    settings['audited_repair_faults']={repair:{'event_id':event['id'],'source':'controlled failure drill','reason':'Correct missing-outcome worker and immutable scope transition'}}
    ok,why=kb.request_review(board,repair,reviewer='reviewer',summary='Native source correction candidate; independent reproduction remains required',metadata={'acceptance_receipts':{'native_authority_correction':{'audit_event_id':event['id'],'scope':'Controlled transition authority only; outcome unproved'}}},resume_audited_repair=True,with_reason=True)
    assert ok,why
    assert kb.get_task(board,repair).status=='review'
    assert kb.get_task(board,repair).result is None
    assert kb.get_task(board,tid).status=='blocked' and kb.get_task(board,tid).block_kind=='dependency'


def test_native_outcome_rejects_structural_receipts_and_returns_to_implementer(board,monkeypatch):
    from hermes_cli import kanban_outcomes as outcomes
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    settings.update(cohort_task_ids=[tid],outcome_checks={tid:{'kind':'queue','url':'http://127.0.0.1:7888/api/questions'}})
    monkeypatch.setattr(outcomes,'check_queue',lambda cfg:{'passed':False,'failures':['Actual duplicate intent remains']})
    assert not kb.complete_task(board,repair,result='Cycle count zero; all repaired',expected_run_id=review.current_run_id,
        metadata={'acceptance_receipts':{'native_original_outcome':{'passed':True},'sql':'zero cycles'}})
    assert kb.get_task(board,repair).status=='ready'
    assert kb.get_task(board,repair).assignee=='pilot'
    assert operator._last(board,repair,'completed') is None
    event=operator._last(board,repair,'operator_outcome_failed')
    assert event['run_id']==review.current_run_id
    assert operator._payload(event)['original_task_id']==tid
    assert 'Actual duplicate intent remains' in operator._payload(operator._last(board,repair,'changes_requested'))['reason']


def test_native_outcome_records_current_probe_not_worker_receipt(board,monkeypatch):
    from hermes_cli import kanban_outcomes as outcomes
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    settings.update(cohort_task_ids=[tid],outcome_checks={tid:{'kind':'queue','url':'http://127.0.0.1:7888/api/questions'}})
    monkeypatch.setattr(outcomes,'check_queue',lambda cfg:{'passed':True,'observed':'controlled current actual API'})
    assert kb.complete_task(board,repair,result='Controlled independently reproduced effect',expected_run_id=review.current_run_id,metadata={'acceptance_receipts':['worker structural claims']})
    native=operator._payload(operator._last(board,repair,'completed'))['acceptance_receipts']['native_original_outcome']
    assert native['passed'] and native['review_run_id']==review.current_run_id
    assert native['observed']=='controlled current actual API'


def test_audited_original_recovery_accepts_actual_old_hold_new_framework_stop(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    settings['cohort_task_ids']=[tid]
    assert kb.complete_task(board,repair,result='Controlled independent existing repair',expected_run_id=review.current_run_id,metadata={'acceptance_receipts':{'probe':'controlled fixture'}})
    hold=operator._last_hold(board,tid)
    with kb.write_txn(board):
        kb._append_event(board,tid,'operator_acceptance_invalidated',{'owner':'agent','source_completed_event_id':123})
        kb._append_event(board,tid,'operator_request_stopped',{'owner':'agent','due_at':1})
        board.execute("UPDATE tasks SET status='blocked',block_kind='capability',result=NULL,claim_lock=NULL,current_run_id=NULL WHERE id=?",(tid,))
    stop=operator._last(board,tid,'operator_request_stopped')
    settings['agent_owned_faults']={tid:{'completed_event_id':123,'blocked_event_id':hold['id'],'deadline_stop_event_id':stop['id'],'retry_acceptance':True,'source':'actual retained old hold and new stop','reason':'controlled real schema'}}
    due=operator._payload(operator._last(board,tid,'operator_deadline'))['due_at']
    monkeypatch.setattr(kb.time,'time',lambda:due+1)
    operator.reconcile(board,settings=settings,now=due+1)
    assert operator._last(board,tid,'operator_audited_acceptance_recovery') is not None
    assert kb.get_task(board,tid).status=='review'
    assert operator._last_hold(board,tid)['id']==hold['id']


@pytest.mark.parametrize('questions,passed',[
    ([{'id':'a','text':'Instagram login cookies'},{'id':'b','text':'Instagram session access'}],False),
    ([{'id':'a','text':'Instagram login cookies'}],True),
    ([{'id':'a','text':'Cycle repair complete. How proceed?'}],False),
    ([{'id':'a','text':'Test: dash reasoning (repair verification question)'}],False),
    ([{'id':'a','text':'Please open the dash queue and verify it'}],False),
    ([{'id':'a','text':'Choose your clip priority','options':['Now','Later']}],True),
])
def test_queue_outcome_checks_intent_and_agent_demands_not_card_ids(monkeypatch,questions,passed):
    from hermes_cli import kanban_outcomes as outcomes
    monkeypatch.setattr(outcomes,'_local_json',lambda url:{'questions':questions})
    assert outcomes.check_queue({'url':'http://127.0.0.1:7888/api/questions'})['passed']==passed


@pytest.mark.parametrize('runtime,passed',[
    ({'pid':1,'source_sha256':'current','credential_sha256':'current-token'},True),
    ({'pid':1,'source_sha256':'old','credential_sha256':'current-token'},False),
    ({'pid':1,'source_sha256':'current','credential_sha256':'inherited-old-token'},False),
    ({},False),
])
def test_dashboard_acceptance_requires_actual_consumer_identity(monkeypatch,runtime,passed):
    from hermes_cli import kanban_outcomes as outcomes
    from types import SimpleNamespace
    monkeypatch.setattr(outcomes.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=0,stdout=json.dumps({'passed':True,'actor':'operator-verification'})))
    monkeypatch.setattr(outcomes,'_local_json',lambda url:{'hermes_client_runtime':runtime})
    result=outcomes.check_conversation({'python':'python','client_sha256':'current','ui_credential_sha256':'current-token','health_url':'http://127.0.0.1:7888/api/health'},dashboard=True)
    assert result['passed']==passed


def test_declared_outcome_rechecks_legacy_claim_once_without_native_probe_event(board,monkeypatch):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    settings['cohort_task_ids']=[tid]
    assert kb.complete_task(board,repair,result='Controlled old structural repair',expected_run_id=review.current_run_id,metadata={'acceptance_receipts':{'probe':'controlled old SQL'}})
    operator.reconcile(board,settings=settings)
    build=kb.claim_task(board,tid)
    assert kb.request_review(board,tid,reviewer='reviewer',summary='controlled candidate',expected_run_id=build.current_run_id)
    review=kb.claim_review_task(board,tid)
    assert kb.complete_task(board,tid,result='Controlled legacy structural result',expected_run_id=review.current_run_id,metadata={'acceptance_receipts':{'native_original_outcome':{'passed':True}}})
    old=operator._last(board,tid,'completed')
    settings['outcome_checks']={tid:{'kind':'queue','url':'http://127.0.0.1:7888/api/questions'}}
    operator.reconcile(board,settings=settings)
    invalid=operator._last(board,tid,'operator_acceptance_invalidated')
    assert invalid and operator._payload(invalid)['source_completed_event_id']==old['id']
    assert kb.get_task(board,tid).status=='review'
    count=board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='review_requested'",(tid,)).fetchone()[0]
    operator.reconcile(board,settings=settings)
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='review_requested'",(tid,)).fetchone()[0]==count


@pytest.mark.parametrize('mode',['exact','stale_phase','human'])
def test_audited_acceptance_deadline_stop_uses_exact_native_phase_without_false_completion(board,monkeypatch,mode):
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    settings['cohort_task_ids']=[tid]
    assert kb.complete_task(board,repair,result='Controlled existing independently accepted repair',expected_run_id=review.current_run_id,metadata={'acceptance_receipts':{'probe':'fixture'}})
    due=operator._payload(operator._last(board,tid,'operator_deadline'))['due_at']
    monkeypatch.setattr(kb.time,'time',lambda:due+1)
    hold=operator._last_hold(board,tid)
    with kb.write_txn(board):
        kb._append_event(board,tid,'operator_acceptance_recovery',{'owner':'agent','due_at':due,'acceptance':'unproved'})
        kb._append_event(board,tid,'operator_acceptance_stopped',{'owner':'agent','due_at':due})
        board.execute("UPDATE tasks SET status='blocked',block_kind=?,result=NULL,claim_lock=NULL,current_run_id=NULL WHERE id=?",('needs_input' if mode=='human' else 'capability',tid))
    phase=operator._last(board,tid,'operator_acceptance_recovery');stop=operator._last(board,tid,'operator_acceptance_stopped')
    assert stop['kind']=='operator_acceptance_stopped'
    settings['agent_owned_faults']={tid:{'blocked_event_id':hold['id'],'deadline_stop_event_id':stop['id'],'acceptance_phase_event_id':phase['id']-(1 if mode=='stale_phase' else 0),'retry_acceptance':True,'source':'actual acceptance deadline record','reason':'bounded independently audited native check correction'}}
    operator.reconcile(board,settings=settings,now=due+1)
    audit=operator._last(board,tid,'operator_audited_acceptance_recovery')
    if mode!='exact':
        assert audit is None
    else:
        assert audit and operator._payload(audit)['source_fault_event_id']==stop['id']
        assert kb.get_task(board,tid).status=='review'
        assert operator._payload(operator._last(board,tid,'operator_deadline'))['due_at']==due


def test_sealed_original_outcome_defaults_guard_before_policy_update():
    from hermes_cli import kanban_outcomes as outcomes
    assert outcomes.declared_check({'cohort_task_ids':['t_2932862d']},'t_2932862d')['kind']=='dashboard'
    assert outcomes.declared_check({'cohort_task_ids':['t_5cb8e6be']},'t_5cb8e6be')['kind']=='conversation'
    assert outcomes.declared_check({'cohort_task_ids':['t_bbaf4f71']},'t_bbaf4f71')['kind']=='queue'


def test_sealed_defaults_preserve_outside_cohort_work():
    from hermes_cli import kanban_outcomes as outcomes
    assert outcomes.declared_check({'cohort_task_ids':[]},'t_2932862d') is None
    assert outcomes.declared_check({'cohort_task_ids':['unrelated']},'unrelated') is None


def test_native_current_effects_supply_receipt_without_worker_format_barrier(board,monkeypatch):
    from hermes_cli import kanban_outcomes as outcomes
    tid,repair,review,settings=_owned_repair_candidate(board,monkeypatch)
    settings.update(cohort_task_ids=[tid],outcome_checks={tid:{'kind':'queue','url':'http://127.0.0.1:7888/api/questions'}})
    monkeypatch.setattr(outcomes,'check_queue',lambda cfg:{'passed':True,'observed':'controlled actual queue outcome'})
    assert kb.complete_task(board,repair,result='Actual effects independently reproduced',expected_run_id=review.current_run_id)
    receipt=operator._payload(operator._last(board,repair,'completed'))['acceptance_receipts']
    assert receipt['native_original_outcome']['passed'] and receipt['worker_observations'] is None


@pytest.mark.parametrize('questions,passed',[
    ([],False),
    ([{'id':'new-clear-title','source_task':'protected-choice','text':'Choose a physical microphone','options':['Connect mic','Park']}],True),
])
def test_queue_cannot_pass_by_erasing_unresolved_harry_only_choice(monkeypatch,questions,passed):
    from hermes_cli import kanban_outcomes as outcomes
    monkeypatch.setattr(outcomes,'_local_json',lambda url:{'questions':questions})
    assert outcomes.check_queue({'url':'http://127.0.0.1:7888/api/questions','preserve_pending_choice_tasks':['protected-choice']})['passed']==passed
