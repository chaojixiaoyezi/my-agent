"""定时任务 waiting 死锁的修复：一轮结束后任务仍是 active 时，只有结构化后续工作事实存在才进 waiting。

背景（2026-09-29 my-agent-2/4）：run_command 结果未知 → 回合 unfinished → 任务仍 active → 旧逻辑无条件 park_waiting，
而 waiting 只在任务终态时才对账、同一 job 有 run 就跳过派发 → 定时任务永久停摆。
这里用真实 SimpleAgent、真实调度账本和会话存储验证：没有后续工作就把任务标为 blocked、run 记 failed、排宿主提示，
job 下一周期照常派发；有后续工作照常 waiting（对照）；存量 waiting 经对账出口解开；判定只读结构化事实。
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.host_notices import pending_host_notices
from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport
from agent_py_agent.agent.conversation.runtime import _finish_scheduler_wake_claim
from agent_py_agent.agent.conversation.task_follow_up import (
    FOLLOW_UP_ACTIVE_GOAL,
    FOLLOW_UP_ENABLED_POLICY,
    FOLLOW_UP_OPEN_SUBAGENTS,
    FOLLOW_UP_PENDING_GUIDANCE,
    FOLLOW_UP_PENDING_PROCESS,
    FOLLOW_UP_PENDING_WAKES,
    FOLLOW_UP_TASK_UNAVAILABLE,
    FollowUpFacts,
    task_follow_up_facts,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler.active_run_closeout import (
    SCHEDULED_TASK_FOLLOW_UP_UNREADABLE,
    SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN,
    SCHEDULED_TASK_UNFINISHED,
    SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP,
)
from agent_py_agent.agent.scheduler.service import (
    SchedulerRunClaim,
    SchedulerService,
    scheduler_terminal_status_for_task,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_scheduler_runtime import _create_job

BROKEN_REPORT = {"category": "data_corruption", "error_type": "DataCorruptionError", "path": ""}
BROKEN_CODE = "data_corruption:DataCorruptionError"
NEXT_PERIOD = 1_600  # every 600 s，锚点 1000：预约第一次时 next_run_at 已推进到 1600
GRACE = 600.0
UNREADABLE_DEADLINE = 6 * GRACE


def _setup(tmp_path):
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "internal", "channel_conversation_id": "thread-diag",
        "channel_user_id": "user-1", "now": 900.0,
    })
    _create_job(agent, thread.thread_id, "diag")
    run = agent.scheduler_repository.reserve_due_runs(now=1_000)[0]
    claimed = agent.scheduler_repository.claim_run(str(run["run_id"]), lease_seconds=300, now=1_001)
    claim = SchedulerRunClaim(run_id=str(run["run_id"]), claim_id=str(claimed["claim_id"]), run=claimed)
    agent.scheduler_repository.mark_run_running(claim.run_id, claim.claim_id, now=1_002)
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": claim.run_id, "goal": "定时诊断", "now": 1_002})
    return agent, store, thread, claim


def _finish(agent, thread, claim, **fields):
    wake_id = fields.pop("wake_id", "wake-diag")
    scheduler = fields.pop("scheduler", None) or SimpleNamespace(scheduler_service=agent.scheduler_service,
                                                                 _wake_retry_after={})
    report = BackgroundMainAgentReport(
        thread_id=thread.thread_id, task_id=claim.run_id, reason="scheduled_job_due", response="本轮停下",
        route_channel="internal", route_target="", created_at=1_003, wake_handled=True, task_status="active",
        **fields,
    )
    returned = _finish_scheduler_wake_claim(scheduler, SimpleNamespace(wake_signal_id=wake_id), report,
                                            claim=claim, now=1_003)
    return report, returned


def _lifecycle_wake(store, thread, claim):
    return store.wakes.raise_signal({"thread_id": thread.thread_id, "root_task_id": claim.run_id,
                                     "reason": "subagent_runner_finished", "urgency": "normal", "now": 1_002})


def _history(agent):
    history, errors = agent.scheduler_repository.history(limit=10)
    assert errors == []
    return history


class TestCloseActiveRun:
    def test_unknown_tool_outcome_without_follow_up_blocks_task_and_keeps_the_job_running(self, tmp_path):
        agent, store, thread, claim = _setup(tmp_path)
        report, returned = _finish(agent, thread, claim, runtime_status="unfinished",
                                   runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
        assert returned is report
        assert store.tasks.load(claim.run_id).status == "blocked"
        assert agent.scheduler_repository.get_active_run(claim.run_id) is None
        [row] = _history(agent)
        assert (row["status"], row["error_code"]) == ("failed", SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN)
        [notice] = pending_host_notices(store, thread.thread_id)
        assert (notice.source, notice.code) == (f"scheduler:{row['job_id']}", SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN)
        assert "无法确认" in notice.text and "重做" in notice.text
        [next_run] = agent.scheduler_repository.reserve_due_runs(now=NEXT_PERIOD)
        assert next_run["run_id"] != claim.run_id and next_run["job_id"] == row["job_id"]

    def test_positive_control_real_follow_up_keeps_waiting_and_blocks_next_dispatch(self, tmp_path):
        agent, store, thread, claim = _setup(tmp_path)
        _lifecycle_wake(store, thread, claim)
        report, returned = _finish(agent, thread, claim, runtime_status="unfinished",
                                   runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
        assert returned is report
        assert agent.scheduler_repository.get_active_run(claim.run_id)["status"] == "waiting"
        assert store.tasks.load(claim.run_id).status == "active"
        assert pending_host_notices(store, thread.thread_id) == ()
        assert agent.scheduler_repository.reserve_due_runs(now=NEXT_PERIOD) == []

    @pytest.mark.parametrize("reason", ["REPEATED_IDENTICAL_TOOL_FAILURE", "pending_turn_input_invalidation_limit", ""])
    def test_other_unfinished_reasons_use_the_generic_code(self, tmp_path, reason):
        agent, store, thread, claim = _setup(tmp_path)
        _finish(agent, thread, claim, runtime_status="unfinished", runtime_reason=reason)
        [row] = _history(agent)
        assert (row["status"], row["error_code"]) == ("failed", SCHEDULED_TASK_UNFINISHED)
        assert store.tasks.load(claim.run_id).status == "blocked"
        assert pending_host_notices(store, thread.thread_id)[0].code == SCHEDULED_TASK_UNFINISHED

    def test_task_changed_concurrently_only_releases_the_claim(self, tmp_path):
        agent, store, thread, claim = _setup(tmp_path)
        store.tasks.update_status({"task_id": claim.run_id, "status": "completed", "now": 1_003})
        report, returned = _finish(agent, thread, claim, runtime_status="unfinished",
                                   runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
        assert returned is None
        assert store.tasks.load(claim.run_id).status == "completed"
        assert _history(agent) == [] and pending_host_notices(store, thread.thread_id) == ()
        assert agent.scheduler_repository.get_active_run(claim.run_id)["claim_id"] == ""

    def test_the_wake_being_handled_is_not_its_own_follow_up(self, tmp_path):
        agent, store, thread, claim = _setup(tmp_path)
        handled = _lifecycle_wake(store, thread, claim)
        _finish(agent, thread, claim, wake_id=handled.wake_signal_id, runtime_status="unfinished",
                runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
        assert store.tasks.load(claim.run_id).status == "blocked"
        assert [row["error_code"] for row in _history(agent)] == [SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN]

    def test_unreadable_task_authority_only_releases_the_claim(self, tmp_path, monkeypatch):
        agent, store, thread, claim = _setup(tmp_path)

        # 函数用途: 模拟任务权威读写失败。
        def broken(_request):
            raise OSError("task table unreadable")

        monkeypatch.setattr(store.tasks, "update_status", broken)
        scheduler = SimpleNamespace(scheduler_service=agent.scheduler_service, _wake_retry_after={})
        _report, returned = _finish(agent, thread, claim, runtime_status="unfinished",
                                    runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN", scheduler=scheduler)
        assert returned is None
        assert _history(agent) == [] and pending_host_notices(store, thread.thread_id) == ()
        run = agent.scheduler_repository.get_active_run(claim.run_id)
        assert (run["status"], run["claim_id"]) == ("queued", "")
        # 复审 R2：释放后必须和其它释放分支一样退避 30 秒，否则磁盘满时每拍都会重新领取、再跑一整片模型。
        assert scheduler._wake_retry_after == {"wake-diag": 1_003 + 30.0}

    def test_a_successful_close_sets_no_backoff(self, tmp_path):
        agent, _store, thread, claim = _setup(tmp_path)
        scheduler = SimpleNamespace(scheduler_service=agent.scheduler_service, _wake_retry_after={})
        _finish(agent, thread, claim, runtime_status="unfinished", runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN",
                scheduler=scheduler)
        assert scheduler._wake_retry_after == {}

    def test_service_without_follow_up_query_fails_closed_to_waiting(self, tmp_path):
        agent, store, thread, claim = _setup(tmp_path)
        agent.scheduler_service = SchedulerService(agent.scheduler_repository, conversation_store=store)
        _finish(agent, thread, claim, runtime_status="unfinished", runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
        assert agent.scheduler_repository.get_active_run(claim.run_id)["status"] == "waiting"
        assert store.tasks.load(claim.run_id).status == "active"


# 复审 R1：后续工作事实读不出来时不能永远 waiting。坏记录能归属到别的任务就不计入；归属不明的按读不出处理，
# 先继续等，停满宽限期的 6 倍才结算为 SCHEDULED_TASK_FOLLOW_UP_UNREADABLE。
class TestUnreadableFollowUp:
    def _corrupt_wake(self, store, content="{truncated"):
        bad = store.storage.wake_queue_dir / "normal" / "wake-of-another-thread.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(content, encoding="utf-8")

    def test_an_unattributable_corrupt_wake_waits_then_settles_at_six_grace_periods(self, tmp_path):
        agent, store, thread, claim = _setup(tmp_path)
        self._corrupt_wake(store)
        _finish(agent, thread, claim, runtime_status="unfinished", runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
        waiting = agent.scheduler_repository.get_active_run(claim.run_id)
        assert waiting["status"] == "waiting" and store.tasks.load(claim.run_id).status == "active"
        since = float(waiting["waiting_since"])
        assert agent.scheduler_service.reconcile_waiting_run(claim.run_id, now=since + UNREADABLE_DEADLINE - 1) is None
        assert agent.scheduler_repository.get_active_run(claim.run_id)["status"] == "waiting"
        assert agent.scheduler_service.reconcile_waiting_run(claim.run_id, now=since + UNREADABLE_DEADLINE) is not None
        [row] = _history(agent)
        assert (row["status"], row["error_code"]) == ("failed", SCHEDULED_TASK_FOLLOW_UP_UNREADABLE)
        assert store.tasks.load(claim.run_id).status == "blocked"
        assert pending_host_notices(store, thread.thread_id)[0].code == SCHEDULED_TASK_FOLLOW_UP_UNREADABLE
        assert len(agent.scheduler_repository.reserve_due_runs(now=NEXT_PERIOD)) == 1

    def test_a_present_wake_is_not_hidden_by_a_corrupt_one_even_after_six_grace_periods(self, tmp_path):
        agent, store, thread, claim = _setup(tmp_path)
        _lifecycle_wake(store, thread, claim)
        _finish(agent, thread, claim, runtime_status="unfinished", runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
        self._corrupt_wake(store)
        since = float(agent.scheduler_repository.get_active_run(claim.run_id)["waiting_since"])
        assert agent.scheduler_service.reconcile_waiting_run(claim.run_id, now=since + UNREADABLE_DEADLINE) is None
        assert agent.scheduler_repository.get_active_run(claim.run_id)["status"] == "waiting"
        assert store.tasks.load(claim.run_id).status == "active"

    def test_a_corrupt_wake_of_another_task_does_not_hold_this_task(self, tmp_path):
        agent, store, thread, claim = _setup(tmp_path)
        self._corrupt_wake(store, '{"root_task_id": "another-task", "thread_id": "t", "wake_signal_id": "w", "created_at": "x"}')
        _finish(agent, thread, claim, runtime_status="unfinished", runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
        [row] = _history(agent)
        assert (row["status"], row["error_code"]) == ("failed", SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN)

    def test_a_real_follow_up_still_wins_over_an_unreadable_item(self, tmp_path):
        agent, store, thread, claim = _setup(tmp_path)
        self._corrupt_wake(store)
        _finish(agent, thread, claim, runtime_status="unfinished", runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
        since = float(agent.scheduler_repository.get_active_run(claim.run_id)["waiting_since"])
        store.goals.create({"thread_id": thread.thread_id, "task_id": claim.run_id, "objective": "持续推进"})
        assert agent.scheduler_service.reconcile_waiting_run(claim.run_id, now=since + 10 * UNREADABLE_DEADLINE) is None
        assert agent.scheduler_repository.get_active_run(claim.run_id)["status"] == "waiting"

    def test_unreadable_items_are_logged_with_their_codes_once_per_interval(self, tmp_path, monkeypatch, caplog):
        from agent_py_agent.agent.scheduler import active_run_closeout

        monkeypatch.setattr(active_run_closeout, "_unreadable_warned_at", {})
        agent, store, thread, claim = _setup(tmp_path)
        self._corrupt_wake(store)
        caplog.set_level("WARNING", logger=active_run_closeout.__name__)
        _finish(agent, thread, claim, runtime_status="unfinished", runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
        since = float(agent.scheduler_repository.get_active_run(claim.run_id)["waiting_since"])
        logged = []
        for offset in (GRACE, GRACE + 1, GRACE + 2, 2 * GRACE + 1):
            agent.scheduler_service.reconcile_waiting_run(claim.run_id, now=since + offset)
            logged.append(len([r for r in caplog.records if getattr(r, "event", "") == "scheduler_follow_up_unreadable"]))
        # 收口时一条；满宽限期的对账再一条；之后同一节流窗口里的两次不打；过了窗口再一条。
        assert logged == [2, 2, 2, 3]
        records = [r for r in caplog.records if getattr(r, "event", "") == "scheduler_follow_up_unreadable"]
        assert {record.run_id for record in records} == {claim.run_id}
        assert records[0].unreadable == [{"item": FOLLOW_UP_PENDING_WAKES, "error_code": "data_parse:JSONDecodeError"}]


def test_follow_up_unreadable_code_is_registered_for_manual_review():
    from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
    from agent_py_agent.agent.contracts.recovery import RecoveryAction

    contract = ERROR_CONTRACTS[SCHEDULED_TASK_FOLLOW_UP_UNREADABLE]
    assert (contract.code, contract.retryable) == (SCHEDULED_TASK_FOLLOW_UP_UNREADABLE, False)
    assert contract.recommended_action == RecoveryAction.MANUAL_REVIEW.value


class TestStaleWaitingExit:
    def _parked(self, tmp_path):
        agent, store, thread, claim = _setup(tmp_path)
        wake = _lifecycle_wake(store, thread, claim)
        _finish(agent, thread, claim, runtime_status="unfinished", runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
        waiting = agent.scheduler_repository.get_active_run(claim.run_id)
        assert waiting["status"] == "waiting"
        return agent, store, thread, claim, wake, float(waiting["waiting_since"])

    def test_waiting_without_follow_up_is_settled_after_the_grace_period(self, tmp_path):
        agent, store, thread, claim, wake, since = self._parked(tmp_path)
        store.wakes.mark_handled(wake.wake_signal_id, now=since)
        assert agent.scheduler_service.reconcile_waiting_run(claim.run_id, now=since + GRACE - 1) is None
        assert agent.scheduler_repository.get_active_run(claim.run_id)["status"] == "waiting"
        assert agent.scheduler_service.reconcile_waiting_runs(now=since + GRACE) == [claim.run_id]
        [row] = _history(agent)
        assert (row["status"], row["error_code"]) == ("failed", SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP)
        assert store.tasks.load(claim.run_id).status == "blocked"
        assert pending_host_notices(store, thread.thread_id)[0].code == SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP
        assert len(agent.scheduler_repository.reserve_due_runs(now=NEXT_PERIOD)) == 1

    def test_waiting_with_follow_up_stays_even_after_the_grace_period(self, tmp_path):
        agent, store, thread, claim, _wake, since = self._parked(tmp_path)
        assert agent.scheduler_service.reconcile_waiting_run(claim.run_id, now=since + 10 * GRACE) is None
        assert agent.scheduler_repository.get_active_run(claim.run_id)["status"] == "waiting"
        assert store.tasks.load(claim.run_id).status == "active"

    def test_waiting_run_whose_task_was_blocked_settles_as_blocked(self, tmp_path):
        agent, store, thread, claim, _wake, since = self._parked(tmp_path)
        store.tasks.update_status({"task_id": claim.run_id, "status": "blocked", "now": since})
        assert agent.scheduler_service.reconcile_waiting_run(claim.run_id, now=since + 1) is not None
        [row] = _history(agent)
        assert (row["status"], row["error_code"]) == ("failed", "SCHEDULED_TASK_BLOCKED")

    def test_blocked_maps_to_a_failed_run(self):
        assert scheduler_terminal_status_for_task("blocked") == "failed"
        assert scheduler_terminal_status_for_task("active") is None


# 函数用途: 组装一个只带任务判定所需属性的 agent：真实会话存储，子代理与进程查询按参数替身。
def _fact_agent(tmp_path, *, runs=(), statuses=None):
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "u", "channel": "internal", "channel_conversation_id": "t",
        "channel_user_id": "u", "now": 1.0,
    })
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-1", "goal": "g", "now": 1.0})
    agent.subagent_run_ids_for_request = lambda task_id: list(runs)
    agent.subagents = SimpleNamespace(list_runs=lambda: [SimpleNamespace(id=run, status=(statuses or {}).get(run, "running"))
                                                         for run in runs])
    return agent, store, thread


class TestFollowUpFacts:
    def test_a_quiet_task_has_no_facts(self, tmp_path):
        agent, _store, thread = _fact_agent(tmp_path)
        assert task_follow_up_facts(agent, thread.thread_id, "task-1") == FollowUpFacts()

    def test_active_goal_counts_but_a_blocked_goal_does_not(self, tmp_path):
        agent, store, thread = _fact_agent(tmp_path)
        goal = store.goals.create({"thread_id": thread.thread_id, "task_id": "task-1", "objective": "持续推进"})
        assert FOLLOW_UP_ACTIVE_GOAL in task_follow_up_facts(agent, thread.thread_id, "task-1").present
        store.goals.update({"thread_id": thread.thread_id, "goal_id": goal.goal_id, "status": "blocked"})
        assert FOLLOW_UP_ACTIVE_GOAL not in task_follow_up_facts(agent, thread.thread_id, "task-1").present

    def test_pending_guidance_for_the_task_counts(self, tmp_path):
        agent, store, thread = _fact_agent(tmp_path)
        store.guidance.append({"target_type": "task", "target_id": "task-1", "message": "继续", "now": 2.0})
        assert task_follow_up_facts(agent, thread.thread_id, "task-1") == FollowUpFacts((FOLLOW_UP_PENDING_GUIDANCE,))

    def test_open_subagents_count_but_ended_ones_do_not(self, tmp_path):
        agent, _store, thread = _fact_agent(tmp_path, runs=("child-1",))
        assert task_follow_up_facts(agent, thread.thread_id, "task-1") == FollowUpFacts((FOLLOW_UP_OPEN_SUBAGENTS,))
        ended, _store2, thread2 = _fact_agent(tmp_path / "ended", runs=("child-1",), statuses={"child-1": "DONE"})
        assert task_follow_up_facts(ended, thread2.thread_id, "task-1") == FollowUpFacts()

    def test_pending_wakes_count_except_the_trigger_and_ignored_ids(self, tmp_path):
        agent, store, thread = _fact_agent(tmp_path)
        trigger = store.wakes.raise_signal({"thread_id": thread.thread_id, "root_task_id": "task-1",
                                            "reason": "scheduled_job_due", "now": 2.0})
        assert task_follow_up_facts(agent, thread.thread_id, "task-1") == FollowUpFacts()
        lifecycle = store.wakes.raise_signal({"thread_id": thread.thread_id, "root_task_id": "task-1",
                                              "reason": "managed_process_exited", "now": 3.0})
        assert task_follow_up_facts(agent, thread.thread_id, "task-1") == FollowUpFacts((FOLLOW_UP_PENDING_WAKES,))
        ignored = (lifecycle.wake_signal_id, trigger.wake_signal_id)
        assert task_follow_up_facts(agent, thread.thread_id, "task-1", ignored) == FollowUpFacts()
        assert task_follow_up_facts(agent, thread.thread_id, "task-2") == FollowUpFacts()

    def test_pending_process_completion_counts(self, tmp_path, monkeypatch):
        from agent_py_agent.agent.conversation import process_events

        agent, _store, thread = _fact_agent(tmp_path)
        monkeypatch.setattr(process_events, "task_has_pending_process_completion", lambda _agent, task: task == "task-1")
        assert task_follow_up_facts(agent, thread.thread_id, "task-1") == FollowUpFacts((FOLLOW_UP_PENDING_PROCESS,))

    def test_enabled_progress_policy_counts(self, tmp_path):
        agent, store, thread = _fact_agent(tmp_path)
        store.progress.create({"thread_id": thread.thread_id, "task_id": "task-1", "interval_seconds": 60,
                               "route_channel": "internal", "route_target": "t", "now": 2.0})
        assert task_follow_up_facts(agent, thread.thread_id, "task-1") == FollowUpFacts((FOLLOW_UP_ENABLED_POLICY,))

    def test_a_policy_for_another_task_does_not_count(self, tmp_path):
        agent, store, thread = _fact_agent(tmp_path)
        store.progress.create({"thread_id": thread.thread_id, "task_id": "task-2", "interval_seconds": 60,
                               "route_channel": "internal", "route_target": "t", "now": 2.0})
        assert task_follow_up_facts(agent, thread.thread_id, "task-1") == FollowUpFacts()

    def test_unreadable_progress_policies_fail_closed(self, tmp_path, monkeypatch):
        agent, store, thread = _fact_agent(tmp_path)
        monkeypatch.setattr(store.progress, "list_report", lambda **_kwargs: ([], [BROKEN_REPORT]))
        assert task_follow_up_facts(agent, thread.thread_id, "task-1") == FollowUpFacts(
            unreadable=((FOLLOW_UP_ENABLED_POLICY, BROKEN_CODE),))

    def test_an_unreadable_source_fails_closed(self, tmp_path, monkeypatch):
        agent, store, thread = _fact_agent(tmp_path)
        monkeypatch.setattr(store.wakes, "pending_report", lambda **_kwargs: ([], [BROKEN_REPORT]))
        assert task_follow_up_facts(agent, thread.thread_id, "task-1") == FollowUpFacts(
            unreadable=((FOLLOW_UP_PENDING_WAKES, BROKEN_CODE),))

    def test_a_failing_subagent_read_is_unreadable_not_an_open_subagent(self, tmp_path):
        agent, _store, thread = _fact_agent(tmp_path, runs=("child-1",))

        # 函数用途: 模拟子代理目录整体读不出来。
        def broken():
            raise OSError("subagent workspace unreadable")

        agent.subagents = SimpleNamespace(list_runs=broken)
        facts = task_follow_up_facts(agent, thread.thread_id, "task-1")
        assert facts.present == () and facts.unreadable[0][0] == FOLLOW_UP_OPEN_SUBAGENTS
        assert facts.unreadable[0][1].endswith(":OSError")

    # 拆出会抛错的版本后，会话任务收尾用的原判据行为不变：读失败仍 fail closed 当成还有子代理。
    def test_task_closeout_check_still_fails_closed_while_the_strict_one_raises(self, tmp_path):
        from agent_py_agent.agent.conversation.task_promotion import (
            _conversation_task_has_open_subagents,
            conversation_task_open_subagents_or_raise,
        )

        agent, _store, _thread = _fact_agent(tmp_path, runs=("child-1",))

        # 函数用途: 模拟子代理目录整体读不出来。
        def broken():
            raise OSError("subagent workspace unreadable")

        agent.subagents = SimpleNamespace(list_runs=broken)
        assert _conversation_task_has_open_subagents(agent, "task-1") is True
        with pytest.raises(OSError):
            conversation_task_open_subagents_or_raise(agent, "task-1")

    def test_one_unreadable_item_does_not_hide_the_others(self, tmp_path, monkeypatch):
        agent, store, thread = _fact_agent(tmp_path)
        store.guidance.append({"target_type": "task", "target_id": "task-1", "message": "继续", "now": 2.0})
        monkeypatch.setattr(store.wakes, "pending_report", lambda **_kwargs: ([], [BROKEN_REPORT]))
        assert task_follow_up_facts(agent, thread.thread_id, "task-1") == FollowUpFacts(
            (FOLLOW_UP_PENDING_GUIDANCE,), ((FOLLOW_UP_PENDING_WAKES, BROKEN_CODE),))

    def test_missing_store_or_task_fails_closed(self, tmp_path):
        missing = FollowUpFacts(unreadable=((FOLLOW_UP_TASK_UNAVAILABLE, "state:missing_store_or_task"),))
        assert task_follow_up_facts(SimpleNamespace(), "t", "task-1") == missing
        agent, _store, thread = _fact_agent(tmp_path)
        assert task_follow_up_facts(agent, thread.thread_id, " ") == missing


# 唤醒、进度策略读的是 owner 全量数据：坏记录能解析出属于别的任务就不计入，解析不出或属于本任务的仍记为读不出。
class TestUnreadableScoping:
    @pytest.mark.parametrize(("content", "counted"), [
        ("{truncated", True),
        ('{"root_task_id": "task-1", "thread_id": "t", "wake_signal_id": "w", "created_at": "x"}', True),
        ('{"root_task_id": "another-task", "thread_id": "t", "wake_signal_id": "w", "created_at": "x"}', False),
    ], ids=["unparseable", "this-task", "another-task"])
    def test_corrupt_wake_counts_only_when_it_may_belong_to_the_task(self, tmp_path, content, counted):
        agent, store, thread = _fact_agent(tmp_path)
        bad = store.storage.wake_queue_dir / "normal" / "wake-broken.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(content, encoding="utf-8")
        facts = task_follow_up_facts(agent, thread.thread_id, "task-1")
        assert [item for item, _code in facts.unreadable] == ([FOLLOW_UP_PENDING_WAKES] if counted else [])

    # be 复审 P2：同类坏记录（无法归属）不能遮住本任务确认存在的唤醒或进度策略。
    @pytest.mark.parametrize("source", ["wakes", "policies"])
    def test_a_confirmed_match_wins_over_an_unattributable_corrupt_record(self, tmp_path, source):
        agent, store, thread = _fact_agent(tmp_path)
        if source == "wakes":
            store.wakes.raise_signal({"thread_id": thread.thread_id, "root_task_id": "task-1",
                                      "reason": "managed_process_exited", "now": 3.0})
            bad, expected = store.storage.wake_queue_dir / "normal" / "wake-broken.json", FOLLOW_UP_PENDING_WAKES
        else:
            store.progress.create({"thread_id": thread.thread_id, "task_id": "task-1", "interval_seconds": 60,
                                   "route_channel": "internal", "route_target": "t", "now": 2.0})
            bad, expected = store.storage.policies_dir / "policy-broken.json", FOLLOW_UP_ENABLED_POLICY
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{truncated", encoding="utf-8")
        assert task_follow_up_facts(agent, thread.thread_id, "task-1") == FollowUpFacts((expected,))

    @pytest.mark.parametrize(("task_id", "counted"), [("task-1", True), ("another-task", False)])
    def test_corrupt_policy_counts_only_when_it_may_belong_to_the_task(self, tmp_path, task_id, counted):
        agent, store, thread = _fact_agent(tmp_path)
        bad = store.storage.policies_dir / "policy-broken.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(json.dumps({"task_id": task_id, "next_due_at": "x"}), encoding="utf-8")
        facts = task_follow_up_facts(agent, thread.thread_id, "task-1")
        assert [item for item, _code in facts.unreadable] == ([FOLLOW_UP_ENABLED_POLICY] if counted else [])


def test_host_notice_merges_per_job_so_repeated_failures_do_not_pile_up(tmp_path):
    agent, store, thread, claim = _setup(tmp_path)
    _finish(agent, thread, claim, runtime_status="unfinished", runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
    run = agent.scheduler_repository.reserve_due_runs(now=NEXT_PERIOD)[0]
    claimed = agent.scheduler_repository.claim_run(str(run["run_id"]), lease_seconds=300, now=time.time())
    second = SchedulerRunClaim(run_id=str(run["run_id"]), claim_id=str(claimed["claim_id"]), run=claimed)
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": second.run_id, "goal": "定时诊断", "now": 1_700})
    _finish(agent, thread, second, runtime_status="unfinished", runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
    assert len(pending_host_notices(store, thread.thread_id)) == 1
    assert [row["status"] for row in _history(agent)] == ["failed", "failed"]


def test_notices_from_different_jobs_stay_separate(tmp_path):
    agent, store, thread, claim = _setup(tmp_path)
    _create_job(agent, thread.thread_id, "other")
    run = agent.scheduler_repository.reserve_due_runs(now=1_000)[0]
    claimed = agent.scheduler_repository.claim_run(str(run["run_id"]), lease_seconds=300, now=1_001)
    other = SchedulerRunClaim(run_id=str(run["run_id"]), claim_id=str(claimed["claim_id"]), run=claimed)
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": other.run_id, "goal": "另一个定时", "now": 1_002})
    for current in (claim, other):
        _finish(agent, thread, current, runtime_status="unfinished", runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN")
    assert {notice.source for notice in pending_host_notices(store, thread.thread_id)} == {
        f"scheduler:{claim.run['job_id']}", f"scheduler:{other.run['job_id']}"}


# 函数用途: 用真实后台运行时跑一轮定时执行；模型尝试替换成"工具结果无法确认、回合没做完"的结构化结果。
def _tick_with_unknown_tool_outcome(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.models import AgentRunResult
    from agent_py_agent.agent.conversation import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
        FakeDeliveryService,
    )

    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "internal", "channel_conversation_id": "thread-e2e",
        "channel_user_id": "user-1", "now": 900.0,
    })
    _create_job(agent, thread.thread_id, "diag")
    monkeypatch.setattr(agent, "run", lambda prompt, **_kwargs: AgentRunResult(
        prompt=prompt, response="有一个命令的结果无法确认，已停下。", backend="fake", used_memories=0,
        runtime_status="unfinished", runtime_reason="TOOL_OPERATION_OUTCOME_UNKNOWN"))
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store,
                                              "scheduler_service": agent.scheduler_service})
    return agent, store, thread, scheduler, scheduler.tick(now=1_000)


def test_end_to_end_unknown_tool_outcome_does_not_stop_the_schedule(tmp_path, monkeypatch):
    agent, store, thread, scheduler, reports = _tick_with_unknown_tool_outcome(tmp_path, monkeypatch)
    [report] = reports
    assert (report.runtime_status, report.runtime_reason) == ("unfinished", "TOOL_OPERATION_OUTCOME_UNKNOWN")
    [row] = _history(agent)
    assert (row["status"], row["error_code"]) == ("failed", SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN)
    assert store.tasks.load(str(row["run_id"])).status == "blocked"
    assert pending_host_notices(store, thread.thread_id)[0].code == SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN
    assert len(scheduler.tick(now=NEXT_PERIOD)) == 1
    assert [item["status"] for item in _history(agent)] == ["failed", "failed"]


# 函数用途: 以一条合法记录为底写一份实例字段损坏的坏记录（生产形状），completion_target 按参数改写或删掉；
#   "unparseable" 写成截断的 JSON。确认它真的产生读取错误。
def _write_broken_process_record(authority, valid, target):
    path = authority.root / "bg-broken.json"
    if target == "unparseable":
        path.write_text("{truncated", encoding="utf-8")
    else:
        broken = dict(valid, session_id="bg-broken", launcher_pid="not-an-int")
        if target == "missing":
            broken.pop("completion_target", None)
        else:
            broken["completion_target"] = target if not target else {**valid["completion_target"], **target}
        path.write_text(json.dumps(broken), encoding="utf-8")
    assert authority.list_records()[1], "fixture must actually produce a read error"


class TestPendingProcessCompletion:
    def test_real_process_record_counts_until_its_notice_is_sent(self, tmp_path):
        from agent_py_agent.agent.conversation.process_events import (
            reconcile_process_completions,
            task_has_pending_process_completion,
        )
        from agent_py_agent.tests.test_process_completion_events import _fixture

        agent, params, _authority = _fixture(tmp_path)
        assert task_has_pending_process_completion(agent, params.task_id) is True
        assert task_has_pending_process_completion(agent, "task-other") is False
        assert reconcile_process_completions(agent) == 1
        assert task_has_pending_process_completion(agent, params.task_id) is False

    def test_a_record_for_another_conversation_store_does_not_count(self, tmp_path):
        from agent_py_agent.agent.conversation.process_events import (
            task_has_pending_process_completion,
        )
        from agent_py_agent.agent.conversation.store import ConversationStore
        from agent_py_agent.tests.test_process_completion_events import _fixture

        agent, params, _authority = _fixture(tmp_path)
        other = SimpleNamespace(conversation_store=ConversationStore(tmp_path / "owner" / "other-conversations"),
                                home_paths=agent.home_paths, config=agent.config)
        assert task_has_pending_process_completion(other, params.task_id) is False

    # 坏的后台命令记录：能解析出不属于本任务（没有完成通知目标——缺键或盘上的规范空形状 {}——，或目标指向别的任务 /
    # 别的会话存储）就不计入。底记录先把完成通知发掉，这样判定只能靠坏记录的归属。
    @pytest.mark.parametrize(("target", "raises"), [
        ("unparseable", True),
        ({"task_id": "task-a"}, True),
        ({"task_id": "another-task"}, False),
        ({"task_id": "task-a", "store_root": "/elsewhere"}, False),
        ("missing", False),
        ({}, False),
    ], ids=["unparseable", "this-task", "another-task", "another-store", "missing-target", "empty-target"])
    def test_corrupt_records_count_only_when_they_may_belong_to_the_task(self, tmp_path, target, raises):
        from agent_py_agent.agent.conversation.process_events import (
            reconcile_process_completions,
            task_has_pending_process_completion,
        )
        from agent_py_agent.agent.tooling.process_registry import ProcessSessionAuthorityError
        from agent_py_agent.tests.test_process_completion_events import _fixture

        agent, params, authority = _fixture(tmp_path, managed=True)
        [valid] = authority.list_records()[0]
        assert reconcile_process_completions(agent) == 1
        _write_broken_process_record(authority, valid, target)
        if raises:
            with pytest.raises(ProcessSessionAuthorityError):
                task_has_pending_process_completion(agent, params.task_id)
        else:
            assert task_has_pending_process_completion(agent, params.task_id) is False

    # be 复审 P1/P2：同类坏记录不能遮住本任务确认存在的待通知记录，无论坏记录归属是否可知。
    @pytest.mark.parametrize("target", ["unparseable", {"task_id": "task-a"}, {}], ids=["unparseable", "this-task", "empty"])
    def test_a_confirmed_pending_record_wins_over_a_corrupt_one(self, tmp_path, target):
        from agent_py_agent.agent.conversation.process_events import (
            task_has_pending_process_completion,
        )
        from agent_py_agent.tests.test_process_completion_events import _fixture

        agent, params, authority = _fixture(tmp_path, managed=True)
        [valid] = authority.list_records()[0]
        _write_broken_process_record(authority, valid, target)
        assert task_has_pending_process_completion(agent, params.task_id) is True

    def test_unreadable_process_authority_raises_instead_of_reporting_none(self, tmp_path, monkeypatch):
        from agent_py_agent.agent.conversation.process_events import (
            task_has_pending_process_completion,
        )
        from agent_py_agent.agent.tooling.process_registry import ProcessSessionAuthorityError
        from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
        from agent_py_agent.tests.test_process_completion_events import _fixture

        agent, params, _authority = _fixture(tmp_path)
        monkeypatch.setattr(ProcessSessionStore, "list_records", lambda self: ([], [{"code": "BROKEN"}]))
        with pytest.raises(ProcessSessionAuthorityError):
            task_has_pending_process_completion(agent, params.task_id)
