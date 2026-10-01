"""Controlled failure drills using actual board APIs and isolated Hermes homes."""
import json
import pytest

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
    from hermes_cli.config import load_config
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban":{"default_assignee":"pilot"}})
    conn = kb.connect()
    yield conn
    conn.close()


def entry():
    return dict(platform="discord", conversation_id="channel", thread_id="thread", user_id="harry",
                message_id="message", message="Produce the weekly briefing", idempotency_key="discord:channel:message")


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


def test_reviewed_repair_resumes_exact_original_task(board):
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
    assert kb.complete_task(board, repair, result="independent reproduction passed", expected_run_id=review.current_run_id)
    assert kb.get_task(board, tid).status == "blocked"
    operator.reconcile(board, settings=settings)
    assert kb.get_task(board, tid).status == "ready"


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
