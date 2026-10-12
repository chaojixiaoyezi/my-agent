"""独立定时工作使用真实仓库、后台执行与冻结交付，模型和外发使用可控替身。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_model_profiles import add
from agent_py_agent.tests.test_scheduler_runtime import _Backend, _RetryDelivery


# LLM: 显式隔离 home，不依赖 pytest 环境夹具；cwd 可与 Gateway 根不同，替身只负责模型文本与外发。
# 函数用途: 创建独立定时执行夹具，覆盖真实会话目录、预检和冻结重投。
def execution_case(tmp_path, *, precheck=None, policy=None, cwd=None, execution=None):
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), enable_tools=False,
        memory_path="memory.jsonl", orphan_supervision_interval_seconds=0), tmp_path)
    backend = _Backend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "user-1", "channel": "feishu",
        "channel_conversation_id": "chat-isolated", "channel_user_id": "open-1", "cwd": str(cwd or tmp_path), "now": 900})
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "不应进入独立运行的原会话正文", "channel": "feishu"})
    job = agent.scheduler_repository.create_job(SchedulerJobCreateRequest(
        name="独立合成检查", prompt="核验合成资料", thread_id=thread.thread_id,
        schedule={"kind": "every", "every_seconds": 600, "anchor_at": 1000, "timezone": "UTC"},
        execution=execution or {"mode": "isolated"}, precheck=precheck, policy=policy, now=900,
    ))[0]
    agent.scheduler_service.enqueue_ready_runs(now=1000)
    signal = store.wakes.pending()[0]
    channels = _RetryDelivery()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": store, "scheduler_service": agent.scheduler_service})
    return SimpleNamespace(agent=agent, backend=backend, store=store, thread=thread, signal=signal,
                           job=job, channels=channels, scheduler=scheduler)


def test_isolated_execution_does_not_load_origin_history_and_redelivery_does_not_rerun(tmp_path):
    case = execution_case(tmp_path)
    run = case.agent.scheduler_repository.get_active_run(case.signal.root_task_id)
    assert run["thread_id"] != case.thread.thread_id
    assert not case.store.threads.require(run["thread_id"]).channel_bindings
    assert case.scheduler.tick(now=1000) == []
    assert len(case.backend.prompts) == 1
    assert "不应进入独立运行的原会话正文" not in case.backend.prompts[0]
    assert case.channels.attempts[0][0].thread_id == case.thread.thread_id
    pending = case.store.wakes.pending_one(case.signal.wake_signal_id)
    assert pending.metadata["owner_delivery"]["content"] == "已按计划完成。"
    case.channels.status = "sent"
    case.scheduler.tick(now=1031)
    assert len(case.backend.prompts) == 1 and len(case.channels.attempts) == 2
    rows = case.store.messages.recent(case.thread.thread_id, limit=0)
    assert len([r for r in rows if r.role == "assistant" and r.content]) == 1
    history, errors = case.agent.scheduler_repository.history()
    assert errors == [] and history[0]["status"] == "done"
    assert case.store.tasks.load(run["run_id"]).thread_id == run["thread_id"]


def test_origin_reset_keeps_frozen_delivery_without_guessing_new_thread(tmp_path):
    case = execution_case(tmp_path)
    case.scheduler.tick(now=1000)
    # 使用原 ThreadStore 创建和绑定变更真实索引，模拟同一通道换到新会话。
    case.store.threads._create({"channel": "feishu", "channel_conversation_id": "chat-isolated", "channel_user_id": "open-1",
                             "canonical_user_id": "user-1", "now": 1010})
    case.channels.status = "sent"
    case.scheduler.tick(now=1031)
    assert len(case.backend.prompts) == 1 and len(case.channels.attempts) == 1
    pending = case.store.wakes.pending_one(case.signal.wake_signal_id)
    assert pending is not None and pending.metadata["owner_delivery"]["content"] == "已按计划完成。"


def test_repeated_materialization_preserves_compact_and_model_selection(tmp_path):
    case = execution_case(tmp_path)
    thread_id = case.signal.thread_id
    case.store.threads.update_atomic(thread_id, lambda row: replace(row, summary="已确认进度", compact_generation=3))
    case.agent.scheduler_service.enqueue_ready_runs(now=1001)
    updated = case.store.threads.require(thread_id)
    assert updated.summary == "已确认进度" and updated.compact_generation == 3
    assert len(case.store.wakes.pending()) == 1


def test_fixed_and_inherited_model_policy_use_original_scope_and_latest_explicit_revision(tmp_path):
    from agent_py_agent.agent.agent_core.model.selection_refresh import refresh_selected_model
    from agent_py_agent.agent.scheduler.execution import scheduled_model_scope
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.agent.settings.model_scope import (
        current_model_selection,
        model_dependency_lifetime,
        thread_model_execution_status,
    )
    from agent_py_agent.tests.test_model_selection_history import _params

    case = execution_case(tmp_path)
    agent = case.agent
    fixed, _ = add(agent, model_name="fixed-profile")
    newer, _ = add(agent, model_name="newer-profile")
    execute_model_profile_operation(agent, "select", {"profile_id": fixed}, thread_id=case.thread.thread_id)
    thread = case.store.threads.require(case.signal.thread_id)
    request = SimpleNamespace(thread_id=thread.thread_id, task_id=case.signal.root_task_id)
    params = replace(_params(), task_attributes={"conversation_thread_id": thread.thread_id})
    with scheduled_model_scope(agent, thread, request), model_dependency_lifetime(agent):
        assert agent.config.model_name == "fixed-profile"
        execute_model_profile_operation(agent, "select", {"profile_id": newer}, thread_id=case.thread.thread_id)
        assert thread_model_execution_status(agent, thread.thread_id)["pending"] is True
        refresh_selected_model(agent, params)
        selected = current_model_selection(agent)
        assert agent.config.model_name == "newer-profile"
        assert selected.selection_thread_id == case.thread.thread_id and selected.thread_id == thread.thread_id
        assert thread_model_execution_status(agent, thread.thread_id)["pending"] is False
    # 新 job 固定指定档案；现有 run 的继承配置没有随 job 编辑被改写。
    repo = agent.scheduler_repository
    job = repo.get_job(case.job["job_id"])
    repo.update_job(job["job_id"], patch={"execution": {"mode": "isolated", "model_policy": "fixed", "profile_id": fixed}}, expected_version=job["version"])
    run = repo.get_active_run(case.signal.root_task_id)
    assert run["execution"]["model_policy"] == "inherit"
    claimed = repo.claim_run(run["run_id"], lease_seconds=30, now=1000)
    from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish
    repo.finish_run(run["run_id"], claimed["claim_id"], SchedulerRunFinish(status="done", now=1001))
    agent.scheduler_service.enqueue_ready_runs(now=1600)
    fixed_run = next(r for r in repo.queued_runs(now=1600) if r["run_id"] != run["run_id"])
    thread = case.store.threads.require(fixed_run["thread_id"])
    request = SimpleNamespace(thread_id=thread.thread_id, task_id=fixed_run["run_id"])
    params = replace(_params(), task_attributes={"conversation_thread_id": thread.thread_id})
    with scheduled_model_scope(agent, thread, request), model_dependency_lifetime(agent):
        refresh_selected_model(agent, params)
        assert agent.config.model_name == "fixed-profile"
        assert current_model_selection(agent).selection_source == "scheduler_fixed"
        status = thread_model_execution_status(agent, thread.thread_id)
        assert status["pending"] is False and status["selected"]["source"] == "scheduler_fixed"
        assert status["selected"]["profile_id"] == fixed


def test_busy_background_lane_does_not_mark_schedule_started_and_queue_deadline_still_applies(tmp_path):
    case = execution_case(tmp_path, policy={"queue_timeout_seconds": 10})
    repo = case.agent.scheduler_repository
    # 使用真实背景车道租约制造竞争；该 run 没有模型或预检开始事实。
    claim = case.store.claims.acquire({"thread_id": case.signal.thread_id, "task_id": "busy-task",
                                      "lease_seconds": 60, "now": 1000})
    assert claim is not None
    case.scheduler.tick(now=1000)
    run = repo.get_active_run(case.signal.root_task_id)
    assert case.backend.prompts == []
    assert run["status"] == "queued" and run["started_at"] == 0
    case.scheduler.tick(now=1011)
    rows, _ = repo.history()
    assert rows[0]["status"] == "skipped" and rows[0]["error_code"] == "SCHEDULER_QUEUE_EXPIRED"
    assert case.store.tasks.load(case.signal.root_task_id).status == "cancelled"
    assert case.backend.prompts == []


def test_failure_recording_execution_start_settles_original_run_without_model(tmp_path, monkeypatch):
    case = execution_case(tmp_path)
    def fail(*args, **kwargs):
        raise OSError("synthetic start write failed")
    monkeypatch.setattr(case.agent.scheduler_repository, "mark_run_running", fail)
    with pytest.raises(OSError, match="synthetic start"):
        case.scheduler.tick(now=1000)
    assert case.backend.prompts == []
    rows, errors = case.agent.scheduler_repository.history()
    assert errors == [] and rows[0]["status"] == "failed"
    assert case.store.wakes.pending_one(case.signal.wake_signal_id) is None


@pytest.mark.parametrize("mode", ["followup", "isolated"])
@pytest.mark.parametrize("ordinary_input", ["none", "present", "missing"])
def test_scheduled_child_wake_resolves_original_run_without_inventing_user_input(tmp_path, mode, ordinary_input):
    import json

    from agent_py_agent.agent.conversation.runtime import (
        BackgroundRunRequest,
        _background_model_inputs,
        _goal_runtime_context,
    )
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    case = execution_case(tmp_path, execution={"mode": mode})
    claim = case.agent.scheduler_service.claim_wake(case.signal, now=1000).claim
    assert claim is not None
    metadata = {"conversation_request_id": claim.run_id}
    if ordinary_input != "none":
        metadata["events"] = [{"metadata": {"conversation_request_id": "ordinary-request"}}]
    if ordinary_input == "present":
        case.store.messages.append({"thread_id": claim.run["thread_id"], "role": "user", "content": "补充报告格式",
                                    "metadata": {"conversation_request_id": "ordinary-request"}})
    request = BackgroundRunRequest(thread_id=claim.run["thread_id"], task_id=claim.run_id,
        reason="subagent_runner_finished", wake_signal={"metadata": metadata})
    if ordinary_input == "missing":
        with pytest.raises(DataCorruptionError, match="background request input is missing"):
            _goal_runtime_context(case.agent, case.store, request)
        return
    context = _goal_runtime_context(case.agent, case.store, request)
    assert context.task_objective == ("补充报告格式" if ordinary_input == "present" else case.job["prompt"])
    _, _, trigger = _background_model_inputs(request, task_objective=context.task_objective,
        wake_prompt="合成子代理完成", task_origin=(context.durable_task_id, context.durable_task_objective))
    facts = json.loads(trigger.event_facts)
    assert facts["origin_task"] == case.job["prompt"]
    assert facts["origin_request_ids"] == (["ordinary-request"] if ordinary_input == "present" else [])
    assert all(row.metadata.get("conversation_request_id") != claim.run_id
               for row in case.store.messages.recent(claim.run["thread_id"], limit=0))


@pytest.mark.parametrize("mode", ["followup", "isolated"])
def test_waiting_schedule_replays_child_completion_into_same_run_and_delivery(tmp_path, mode):
    case = execution_case(tmp_path, execution={"mode": mode})
    service = case.agent.scheduler_service
    claim = service.claim_wake(case.signal, now=1000).claim
    service.repository.mark_run_running(claim.run_id, claim.claim_id, now=1000)
    service.park_waiting(claim, now=1001)
    case.store.wakes.mark_handled(case.signal.wake_signal_id, now=1001)
    case.store.wakes.raise_signal({"thread_id": claim.run["thread_id"], "root_task_id": claim.run_id,
        "reason": "subagent_runner_finished", "now": 1002,
        "metadata": {"conversation_request_id": claim.run_id}})
    case.channels.status = "sent"
    case.scheduler.tick(now=1003)
    case.scheduler.tick(now=1004)
    assert len(case.backend.prompts) == 1
    assert case.store.tasks.load(claim.run_id).status == "completed"
    assert case.channels.attempts[0][0].thread_id == case.thread.thread_id
    history, errors = service.repository.history()
    assert errors == [] and history[0]["run_id"] == claim.run_id and history[0]["status"] == "done"


@pytest.mark.parametrize("mode", ["followup", "isolated"])
@pytest.mark.parametrize("delivery_status", ["sent", "rejected"])
def test_waiting_schedule_does_not_commit_checkpoint_during_final_delivery(tmp_path, monkeypatch, mode, delivery_status):
    case = execution_case(tmp_path, execution={"mode": mode},
        precheck={"tool_name": "read_file", "arguments": {"path": "revision.txt"}})
    service = case.agent.scheduler_service
    claim = service.claim_wake(case.signal, now=1000).claim
    service.repository.mark_run_running(claim.run_id, claim.claim_id, now=1000)
    service.repository.record_precheck(claim.run_id, claim.claim_id,
        {"status": "changed", "fingerprint": "synthetic-fingerprint", "observed_at": 1000})
    service.park_waiting(claim, response="孩子正在处理", delivery_status="not_applicable", now=1001)
    case.store.wakes.mark_handled(case.signal.wake_signal_id, now=1001)
    signal = case.store.wakes.raise_signal({"thread_id": claim.run["thread_id"], "root_task_id": claim.run_id,
        "reason": "subagent_runner_finished", "now": 1002,
        "metadata": {"conversation_request_id": claim.run_id}})
    case.channels.status = delivery_status
    original_deliver = case.channels.deliver
    observed = []

    # LLM: 只在真实后台交付入口插入一次调度 tick 的对账交错，不改 TaskLink 或交付结果。
    # 函数用途: 重现任务已 completed、通道尚未确认时被另一轮调度提前收口的窗口。
    def deliver_with_reconcile(context, envelope):
        assert case.store.tasks.load(claim.run_id).status == "completed"
        settled = service.reconcile_waiting_run(claim.run_id, now=1003)
        observed.append((settled, service.repository.get_job(case.job["job_id"]).get("precheck_checkpoint")))
        return original_deliver(context, envelope)

    monkeypatch.setattr(case.channels, "deliver", deliver_with_reconcile)
    case.scheduler.tick(now=1003)
    assert observed == [(None, None)]
    if delivery_status == "rejected":
        assert service.repository.get_active_run(claim.run_id)["status"] == "waiting"
        assert case.store.wakes.pending_one(signal.wake_signal_id).metadata["owner_delivery"]
        assert service.reconcile_waiting_run(claim.run_id, now=1010) is None
        case.channels.status = "sent"
        case.scheduler.tick(now=1034)
    assert len(case.backend.prompts) == 1
    history, errors = service.repository.history()
    assert errors == [] and history[0]["status"] == "done"
    assert history[0]["delivery_status"] == "sent"
    assert history[0]["response"] == "已按计划完成。"
    assert service.repository.get_job(case.job["job_id"])["precheck_checkpoint"]["run_id"] == claim.run_id


def test_waiting_final_receipt_survives_restart_and_rejects_uncommitted_or_foreign_reports(tmp_path):
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport
    from agent_py_agent.agent.scheduler.service import SchedulerService

    case = execution_case(tmp_path)
    service = case.agent.scheduler_service
    claim = service.claim_wake(case.signal, now=1000).claim
    service.park_waiting(claim, response="中间回复", now=1001)
    case.store.wakes.mark_handled(case.signal.wake_signal_id, now=1001)
    signal = case.store.wakes.raise_signal({"thread_id": claim.run["thread_id"], "root_task_id": claim.run_id,
        "reason": "subagent_runner_finished", "now": 1002})
    case.store.tasks.update_status({"task_id": claim.run_id, "status": "completed"})
    report = BackgroundMainAgentReport(thread_id=claim.run["thread_id"], task_id=claim.run_id,
        reason=signal.reason, response="最终回复", route_channel="feishu", route_target="chat-isolated",
        created_at=1002, delivery_status="sent", delivery_reason="subagent_completion", wake_handled=True)
    before = service.repository.get_active_run(claim.run_id)
    for invalid in (replace(report, wake_handled=False), replace(report, thread_id="foreign"),
                    replace(report, task_id="foreign")):
        assert service.repository.record_waiting_delivery(invalid, now=1003) is False
        assert service.repository.get_active_run(claim.run_id) == before
    assert service.repository.record_waiting_delivery(report, now=1003)
    restored = SchedulerService(service.repository, conversation_store=case.store, follow_up=service.follow_up)
    assert restored.reconcile_waiting_run(claim.run_id, now=1004) is None
    case.store.wakes.mark_handled(signal.wake_signal_id, now=1005)
    result = restored.reconcile_waiting_run(claim.run_id, now=1006)
    assert (result["status"], result["response"], result["delivery_status"], result["ended_at"]) == (
        "done", "最终回复", "sent", 1006)
    assert restored.reconcile_waiting_run(claim.run_id, now=1007) is None


def test_completed_waiting_task_with_live_executor_does_not_close_without_a_pending_wake(tmp_path):
    from agent_py_agent.agent.conversation.run_claim import detached_task_claim_scope_id

    case = execution_case(tmp_path)
    service = case.agent.scheduler_service
    claim = service.claim_wake(case.signal, now=1000).claim
    service.park_waiting(claim, now=1001)
    case.store.wakes.mark_handled(case.signal.wake_signal_id, now=1001)
    link = case.store.tasks.load(claim.run_id)
    scope = detached_task_claim_scope_id(link.thread_id, link.task_id) if link.cancellation_scope == "detached" else ""
    live = case.store.claims.acquire({"thread_id": link.thread_id, "task_id": link.task_id,
        "claim_scope_id": scope, "reason": "subagent_runner_finished", "lease_seconds": 60, "now": 1002})
    case.store.tasks.update_status({"task_id": claim.run_id, "status": "completed"})
    assert live is not None and service.reconcile_waiting_run(claim.run_id, now=1003) is None
    assert service.repository.get_active_run(claim.run_id)["status"] == "waiting"


@pytest.mark.parametrize("execution", [{"mode": []}, {"model_policy": {}},
    {"model_policy": "fixed"}, {"model_policy": "inherit", "profile_id": "irrelevant"}])
def test_invalid_execution_does_not_write_schedule(tmp_path, execution):
    from agent_py_agent.agent.scheduler.execution_policy import normalize_execution
    from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError

    with pytest.raises(ScheduleValidationError):
        normalize_execution(execution)
