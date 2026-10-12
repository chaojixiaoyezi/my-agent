"""IM 默认后台与 L 显式隔离/模型来源的组合合同；原 Store/调度真实，只有模型和投递替身。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import BackgroundMainAgentScheduler
from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentRuntime
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler.execution import scheduled_model_scope
from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.model_scope import current_model_selection
from agent_py_agent.tests.test_model_profiles import add
from agent_py_agent.tests.test_scheduler_runtime import _Backend, _RetryDelivery


# LLM: 正式工具根据真实 IM binding 创建计划；不手写线程来源或改宿主策略，替身只承接模型/投递运输。
# 函数用途: 建立同 owner 下的 IM 后台计划，验证合并后显式执行策略不会覆盖原布置者。
def im_case(tmp_path, *, execution=None):
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), enable_tools=False,
                                   orphan_supervision_interval_seconds=0), tmp_path)
    backend = _Backend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "user-1", "channel": "feishu",
        "channel_user_id": "ou_a", "channel_conversation_id": "oc_group", "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir), "cwd": str(tmp_path), "now": 900})
    agent._current_run_params = SimpleNamespace(request_id="create-im-plan", task_attributes={"conversation_thread_id": thread.thread_id})
    outcome = ScheduleTool(agent).execute({"action": "create", "name": "合成提醒", "prompt": "整理自己的合成资料",
        "schedule_kind": "every", "every_seconds": 600, "execution": execution or {"mode": "followup"}})
    assert outcome.ok, outcome.output
    job = agent.scheduler_repository.list_jobs()[0][0]
    return SimpleNamespace(agent=agent, store=store, thread=thread, job=job, backend=backend)


def test_default_im_background_inherits_live_source_model_without_returning_to_chat(tmp_path):
    case = im_case(tmp_path)
    assert case.job["thread_id"] != case.thread.thread_id
    profile_id, _ = add(case.agent, model_name="source-new")
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation

    execute_model_profile_operation(case.agent, "select", {"profile_id": profile_id}, thread_id=case.thread.thread_id)
    case.agent.scheduler_service.enqueue_ready_runs(now=case.job["next_run_at"])
    signal = case.store.wakes.pending()[0]
    run = case.agent.scheduler_repository.get_active_run(signal.root_task_id)
    assert run["thread_id"] == case.job["thread_id"] and run["thread_id"] != case.thread.thread_id
    execution_thread = case.store.threads.require(run["thread_id"])
    with scheduled_model_scope(case.agent, execution_thread, SimpleNamespace(thread_id=run["thread_id"], task_id=run["run_id"])):
        assert case.agent.config.model_name == "source-new"
        assert current_model_selection(case.agent).selection_thread_id == case.thread.thread_id


def test_explicit_isolated_im_run_keeps_original_sender_after_group_sender_changes(tmp_path):
    case = im_case(tmp_path, execution={"mode": "isolated"})
    case.store.threads.update_atomic(case.thread.thread_id, lambda thread: replace(thread,
        channel_bindings=tuple(replace(row, channel_user_id="ou_b") for row in thread.channel_bindings)))
    case.agent.scheduler_service.enqueue_ready_runs(now=case.job["next_run_at"])
    signal = case.store.wakes.pending()[0]
    run = case.agent.scheduler_repository.get_active_run(signal.root_task_id)
    assert run["thread_id"] not in {case.job["thread_id"], case.thread.thread_id}
    channels = _RetryDelivery()
    channels.status = "sent"
    runtime = BackgroundMainAgentRuntime(agent=case.agent, store=case.store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": case.store, "scheduler_service": case.agent.scheduler_service})
    reports = scheduler.tick(now=case.job["next_run_at"])
    assert channels.attempts, ([(r.delivery_status, r.delivery_reason) for r in reports], case.agent.scheduler_repository.history()[0])
    assert len(case.backend.prompts) == 1
    assert channels.attempts[0][0].target == "ou_a"


@pytest.mark.parametrize("extra", [{"execution": {"model_policy": "fixed", "profile_id": "absent"}},
                                  {"policy": {"overlap": "invalid"}}, {"precheck": {"tool_name": "read_file"}}])
def test_invalid_complete_policy_does_not_create_another_im_execution_thread(tmp_path, extra):
    case = im_case(tmp_path)
    original = {path.name for path in case.store.storage.threads_dir.glob("*.json")}
    case.agent._current_run_params = SimpleNamespace(request_id="invalid-plan", task_attributes={"conversation_thread_id": case.thread.thread_id})
    outcome = ScheduleTool(case.agent).execute({"action": "create", "name": "不应登记", "prompt": "合成需求",
        "schedule_kind": "every", "every_seconds": 600, **extra})
    assert not outcome.ok
    assert {path.name for path in case.store.storage.threads_dir.glob("*.json")} == original
    assert len(case.agent.scheduler_repository.list_jobs()[0]) == 1
