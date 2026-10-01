"""Controlled failure drills using actual board APIs and isolated Hermes homes."""
import json
import pytest
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
    operator.reconcile(board,settings=settings)
    assert kb.get_task(board,repair).status=='review'
    handoff=operator._last(board,repair,'operator_authority_review_handoff')
    assert operator._payload(handoff)['due_at']>hold['created_at']
    assert kb.claim_review_task(board,repair)
    operator.reconcile(board,settings=settings)
    assert operator._last(board,repair,'operator_authority_review_handoff')['id']==handoff['id']
    assert kb.get_task(board,tid).status=='blocked'
    assert kb.get_task(board,tid).block_kind=='dependency'


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


def _owned_repair_candidate(board,monkeypatch):
    tid=kb.create_task(board,title='failed genuine work',assignee='pilot',user_origin={'platform':'discord','chat_id':'channel','message_id':'message','user_id':'harry','text':entry()['message']})
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
    original_review=kb.claim_review_task(board,tid)
    assert kb.request_changes(board,tid,reason='Original outcome not yet proved',expected_run_id=original_review.current_run_id)[0]
    operator.reconcile(board,settings=settings,now=due+3)
    assert kb.get_task(board,tid).status=='blocked'
    assert kb.get_task(board,tid).block_kind!='needs_input'


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
