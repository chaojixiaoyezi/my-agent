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
def im_case(tmp_path, *, execution=None, delivery=None, group=False):
    config = AgentConfig(my_agent_home=str(tmp_path / "home"), enable_tools=False,
                         orphan_supervision_interval_seconds=0)
    if group:
        config = replace(config, my_agent_owner_provider="feishu", my_agent_owner_kind="group", my_agent_owner_id="oc_group")
    agent = SimpleAgent(config, tmp_path)
    backend = _Backend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "user-1", "channel": "feishu",
        "channel_user_id": "ou_a", "channel_conversation_id": "oc_group", "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir), "cwd": str(tmp_path), "now": 900})
    agent._current_run_params = SimpleNamespace(request_id="create-im-plan", task_attributes={"conversation_thread_id": thread.thread_id})
    outcome = ScheduleTool(agent).execute({"action": "create", "name": "合成提醒", "prompt": "整理自己的合成资料",
        "schedule_kind": "every", "every_seconds": 600, "execution": execution or {"mode": "followup"},
        **({"delivery": delivery} if delivery is not None else {})})
    assert outcome.ok, outcome.output
    job = agent.scheduler_repository.list_jobs()[0][0]
    return SimpleNamespace(agent=agent, store=store, thread=thread, job=job, backend=backend)


@pytest.mark.parametrize("mode", ["followup", "isolated"])
def test_group_plan_freezes_conversation_even_when_actor_is_admin_and_wake_target_is_wrong(tmp_path, mode):
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
    from agent_py_agent.agent.scheduler.execution import resolve_delivery, validate_delivery_origin
    from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity

    case = im_case(tmp_path, execution={"mode": mode}, delivery={"mode": "origin"}, group=True)
    bind_admin_channel_identity(case.agent.home_paths.root, "feishu", "ou_a")
    case.agent.scheduler_service.enqueue_ready_runs(now=case.job["next_run_at"])
    signal = case.store.wakes.pending()[0]
    run = case.agent.scheduler_repository.get_active_run(signal.root_task_id)
    assert run["execution_context"]["origin_bindings"][0]["channel_user_id"] == "ou_a"
    thread = case.store.threads.require(run["thread_id"])
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id=run["run_id"], reason="scheduled_job_due",
                                   route_channel="feishu", route_target="ou_a")
    origin, channel, target = resolve_delivery(case.agent, thread, request, "feishu", "ou_a")
    assert (origin.thread_id, channel, target) == (case.thread.thread_id, "feishu", "oc_group")
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    context = DeliveryContext(thread_id=origin.thread_id, channel=channel, target=target)
    assert validate_delivery_origin(case.agent, request, context)
    assert not validate_delivery_origin(case.agent, request, replace(context, target="ou_a"))
    channels = _RetryDelivery()
    channels.status = "sent"
    runtime = BackgroundMainAgentRuntime(agent=case.agent, store=case.store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({"runtime": runtime, "store": case.store, "scheduler_service": case.agent.scheduler_service})
    scheduler.tick(now=case.job["next_run_at"])
    assert len(channels.attempts) == 1 and channels.attempts[0][0].target == "oc_group"
    history, errors = case.agent.scheduler_repository.history()
    assert not errors and history[0]["delivery_status"] == "sent"


@pytest.mark.parametrize("mode", ["followup", "isolated"])
def test_wrong_group_delivery_context_cannot_call_transport_or_record_sent(tmp_path, mode):
    from agent_py_agent.agent.conversation.background_delivery import (
        BackgroundDeliveryDependencies,
        record_background_response,
    )
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest

    case = im_case(tmp_path, execution={"mode": mode}, delivery={"mode": "origin"}, group=True)
    case.agent.scheduler_service.enqueue_ready_runs(now=case.job["next_run_at"])
    signal = case.store.wakes.pending()[0]
    request = BackgroundRunRequest(thread_id=signal.thread_id, task_id=signal.root_task_id, reason=signal.reason,
                                   wake_signal={"wake_signal_id": signal.wake_signal_id})
    channels = _RetryDelivery()
    channels.status = "sent"
    context = DeliveryContext(thread_id=case.thread.thread_id, channel="feishu", target="ou_a")
    result = record_background_response(BackgroundDeliveryDependencies(case.agent, case.store, channels, lambda _: "completed"),
        request, context, "合成群结果", deliver=True, delivery_reason="scheduled_job_due")
    assert result.delivery_status == "failed" and not channels.attempts
    pending = case.store.wakes.pending_one(signal.wake_signal_id)
    assert pending.metadata["owner_delivery"]["last_delivery_status"] == "failed"
    assert pending.metadata["owner_delivery"]["message_metadata"]["delivery_error_code"] == "SCHEDULER_ORIGIN_BINDING_CHANGED"


@pytest.mark.parametrize("mode", ["followup", "isolated"])
@pytest.mark.parametrize("owner", ["", "providers/feishu/groups/oc_foreign"])
def test_frozen_group_delivery_uses_exact_run_owner_and_rejects_origin_owner_change(tmp_path, mode, owner):
    from agent_py_agent.agent.conversation.channels import DeliveryContext
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
    from agent_py_agent.agent.scheduler.execution import resolve_delivery, validate_delivery_origin

    case = im_case(tmp_path, execution={"mode": mode}, delivery={"mode": "origin"}, group=True)
    case.agent.scheduler_service.enqueue_ready_runs(now=case.job["next_run_at"])
    signal = case.store.wakes.pending()[0]
    case.store.threads.update_atomic(case.thread.thread_id, lambda thread: replace(thread, owner_id=owner))
    request = BackgroundRunRequest(thread_id=signal.thread_id, task_id=signal.root_task_id, reason=signal.reason)
    origin, channel, target = resolve_delivery(case.agent, case.store.threads.require(signal.thread_id), request, 'feishu', 'ou_a')
    assert (channel, target) == ('feishu', 'oc_group')
    context = DeliveryContext(thread_id=origin.thread_id, channel=channel, target=target)
    assert validate_delivery_origin(case.agent, request, context) is (not owner)


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
    case = im_case(tmp_path, execution={"mode": "isolated"}, delivery={"mode": "origin"})
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
