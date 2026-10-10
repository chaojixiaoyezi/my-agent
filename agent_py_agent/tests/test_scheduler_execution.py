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
def execution_case(tmp_path, *, precheck=None, policy=None, cwd=None):
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
        execution={"mode": "isolated"}, precheck=precheck, policy=policy, now=900,
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


@pytest.mark.parametrize("execution", [{"mode": []}, {"model_policy": {}},
    {"model_policy": "fixed"}, {"model_policy": "inherit", "profile_id": "irrelevant"}])
def test_invalid_execution_does_not_write_schedule(tmp_path, execution):
    from agent_py_agent.agent.scheduler.execution_policy import normalize_execution
    from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError

    with pytest.raises(ScheduleValidationError):
        normalize_execution(execution)
