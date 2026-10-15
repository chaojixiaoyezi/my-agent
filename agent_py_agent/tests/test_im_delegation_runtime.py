"""委派执行组合合同：真实 Store/G1/ToolExecutor/创建服务，只有模型和 runner 传输由测试替换。"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.orchestration import lifecycle
from agent_py_agent.agent.agent_core.runtime_mixin import _bind_main_agent_authority
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn
from agent_py_agent.agent.common.cancellation import CancellationToken
from agent_py_agent.agent.conversation.background_execution import (
    BackgroundCapacityDeferred,
    BackgroundExecutionResult,
)
from agent_py_agent.agent.conversation.execution_threads import (
    IM_TASK_EXECUTION_KEY,
    ensure_execution_thread,
)
from agent_py_agent.agent.conversation.im_delegation import (
    prepare_delegated_execution,
    prepare_dispatch_ack,
)
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _close_out_session_task_turn,
    _run_params,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.im_dispatch_policy import IM_DISPATCH_KEY
from agent_py_agent.agent.gateway_parts.im_task_routing import prepare_im_task_dispatch
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


# LLM: 原主执行身份和线程/task 都通过正式入口建立，后台唤醒不启动真实 subprocess；不伪造 provider tool 往返。
# 函数用途: 准备一个管理员 IM 的独立任务和可以执行创建服务的本片参数。
def runtime_fixture(tmp_path, monkeypatch, *, conversation_id='oc_a'):
    agent = SimpleAgent(AgentConfig(prompt_files=[], enable_plugins=False,
                                  my_agent_home=str(tmp_path / "home")), tmp_path / "workspace")
    source = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "ou_a", "channel": "feishu", "channel_user_id": "ou_a",
        "channel_conversation_id": conversation_id, "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir),
    })
    path = tmp_path / "request.json"
    request = {IM_DISPATCH_KEY: {"mode": "delegated", "owner_id": agent.home_paths.owner_id, "preference_revision": 1,
                                "channel": "feishu", "conversation_id": conversation_id, "actor_id": "ou_a"}}
    write_json_file_atomic(path, request)
    context = SimpleNamespace(agent=agent, request=request, request_path=path, request_id="input-one")
    monkeypatch.setattr("agent_py_agent.agent.gateway_parts.im_task_routing._select_route",
                        lambda *_: {"kind": "task", "package_refs": []})
    task = prepare_im_task_dispatch(context, source, "整理工作区里的资料")
    thread = agent.conversation_store.threads.require(task.target_thread_id)
    # 使用真实后台装配入口：首片只有 SessionTask，之后晋升 TaskLink，再把原 actor 交给创建服务。
    background = _run_params(thread.thread_id, BackgroundRunRequest(thread_id=thread.thread_id,
        reason="session_task", host_session_task_id=task.task_id), agent=agent, thread=thread)
    authority = _bind_main_agent_authority(agent, replace(background, run_id="delegate-root",
        attempt_id="delegate-attempt", root_user_prompt=task.goal))
    attrs = authority.task_attributes
    params = ToolLoopExecuteParams(
        user_prompt=task.goal, root_user_prompt=task.goal, memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[], effective_on_chunk=None,
        allowed_tools=None, write_boundary=None, task_attributes=attrs, request_id=task.task_id,
        run_id=authority.run_id, task_id=authority.task_id, attempt_id=authority.attempt_id, one_shot_tool_calls=set(),
        executed_tools=[], archive_tool_calls=[], context_scope="conversation", cancellation_token=CancellationToken(),
        tool_runtime_snapshot=agent.tools.runtime_snapshot(run_id=authority.run_id),
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id=authority.run_id, source_protocol="native"),
    )
    agent._current_run_params = authority
    monkeypatch.setattr(lifecycle, "auto_start_tasks", lambda *_: {"status": "test-deferred"})
    return agent, params, task, context, source


def test_real_executor_creates_one_child_and_reentry_does_not_create_again(tmp_path, monkeypatch):
    agent, params, task, context, source = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    ids = agent.conversation_store.threads.require(task.target_thread_id).metadata[IM_TASK_EXECUTION_KEY]["child_run_ids"]
    assert len(ids) == 1
    assert agent.subagents.load(ids[0]).goal == task.goal
    assert agent.subagents.load(ids[0]).attributes["conversation_request_id"] == task.task_id
    assert "create_subagents" in params.executed_tools
    assert any(isinstance(item, RuntimeFactsTurn) and item.source == "im_task_delegation" for item in params.tool_ir_history)
    assert not params.archive_tool_calls  # 宿主动作不伪造一次模型 tool 往返。
    prepare_delegated_execution(agent, params)
    assert len(agent.subagents.list_runs()) == 1
    assert prepare_im_task_dispatch(context, source, task.goal).task_id == task.task_id


def test_unfinished_slice_does_not_close_session_task(tmp_path, monkeypatch):
    agent, _, task, _, _ = runtime_fixture(tmp_path, monkeypatch)
    agent.conversation_store.session_tasks.bind_turn(task.task_id, turn_id=task.task_id)
    _close_out_session_task_turn(agent, BackgroundRunRequest(thread_id=task.target_thread_id),
                                SimpleNamespace(response="孩子仍在工作", runtime_status="unfinished", error=None), turn_id=task.task_id)
    assert agent.conversation_store.session_tasks.load(task.task_id).status == "accepted"


def test_ack_is_presentation_only_from_real_task_facts(tmp_path, monkeypatch):
    _, params, task, _, _ = runtime_fixture(tmp_path, monkeypatch)
    params.task_attributes["host_im_task_ack"] = {"task_id": task.task_id, "status": "queued"}
    prepare_dispatch_ack(params)
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        natural_user_reply_model_params,
    )

    projected = natural_user_reply_model_params(params)
    assert projected is not params
    assert projected.allowed_tools == []
    assert task.task_id in projected.user_prompt
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        pending_natural_user_reply,
    )

    facts = pending_natural_user_reply(params)['facts']
    assert facts['current_turn_role'] == 'dispatch_acknowledgement'
    assert facts['worker_execution_outcome'] == 'not_observed_in_this_turn'


def test_model_does_not_self_grant_delegation(tmp_path, monkeypatch):
    agent, params, _, _, source = runtime_fixture(tmp_path, monkeypatch)
    params.task_attributes["conversation_thread_id"] = source.thread_id
    params.task_attributes[IM_TASK_EXECUTION_KEY] = {"mode": "delegated"}
    prepare_delegated_execution(agent, params)
    assert not agent.subagents.list_runs()


def test_source_owner_tamper_is_rejected(tmp_path, monkeypatch):
    agent, params, _, _, source = runtime_fixture(tmp_path, monkeypatch)
    agent.conversation_store.threads.update_atomic(source.thread_id, lambda current: replace(current, owner_id="other"))
    with pytest.raises(DataCorruptionError):
        prepare_delegated_execution(agent, params)


def test_capacity_wait_is_not_a_foreground_fallback(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.im_delegation import _start_child

    agent, params, task, _, _ = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    agent.owner_policy = replace(agent.owner_policy, max_subagents=1)
    with pytest.raises(BackgroundCapacityDeferred):
        _start_child(agent, params, replace(task, task_id="second-task", goal="整理另一组资料"), [])
    assert len(agent.subagents.list_runs()) == 1


def test_open_child_request_keeps_original_parent_resolution_and_no_business_tools(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        pending_natural_user_reply,
    )
    from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams

    agent, params, task, _, _ = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    child_id = agent.conversation_store.threads.require(task.target_thread_id).metadata[IM_TASK_EXECUTION_KEY]["child_run_ids"][0]
    agent.subagents.lifecycle.record_capability_request(child_id, RecordCapabilityRequestParams(
        problem="需要登记定时计划", needed_capability="调度工具", requested_tools=["schedule"]))
    params.live_archive_state.clear()
    resolution = prepare_delegated_execution(agent, params)
    assert pending_natural_user_reply(params) is None
    assert resolution.allowed_tools == ["resolve_capability_requests"]
    assert params.allowed_tools is None
    assert any(isinstance(item, RuntimeFactsTurn) and item.source == "im_task_capability_resolution"
               for item in params.tool_ir_history)
    from agent_py_agent.agent.agent_core.orchestration.tools import capability

    monkeypatch.setattr(capability, "_queue_resolved_child_continuation", lambda *a, **k: {"status": "test-deferred"})
    monkeypatch.setattr(capability, "_wake_subagent", lambda *a, **k: None)
    # 工具循环参数是投影；宿主当前 RunParams 保留准确身份，allowed_tools 从原子代理裁决门读取。
    granted = capability.ResolveCapabilityRequestsTool(agent).execute({
        "run_id": child_id, "decision": "grant", "reason": "当前用户具有调度权限"})
    assert granted.ok, granted.output
    assert "schedule" in agent.subagents.load(child_id).allowed_tools


def test_automatic_im_child_inherits_ready_parent_business_tool_in_real_factory(tmp_path, monkeypatch):
    agent, params, _, _, _ = runtime_fixture(tmp_path, monkeypatch)
    assert 'schedule' in params.tool_runtime_snapshot.available_tool_names
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    assert 'schedule' in child.allowed_tools
    assert agent.tools.runtime_snapshot(allowed_tools=child.allowed_tools).runtime('schedule') is not None
    assert not {'create_subagents', 'send_guidance', 'cancel_subagents', 'resolve_capability_requests'} & set(child.allowed_tools)


@pytest.mark.parametrize('owner_kind,access_mode', [('main', 'full-access'), ('main', 'restricted'),
                                                   ('user', 'workspace-write'), ('group', 'workspace-write')])
def test_im_child_uses_current_owner_snapshot_without_inheriting_full_shell(tmp_path, monkeypatch, owner_kind, access_mode):
    owned = SimpleAgent(AgentConfig(prompt_files=[], enable_plugins=False, my_agent_home=str(tmp_path / 'home'),
        my_agent_owner_provider='local' if owner_kind == 'main' else 'feishu', my_agent_owner_kind=owner_kind,
        my_agent_owner_id={'main': 'main', 'user': 'ou_a', 'group': 'oc_a'}[owner_kind],
        access_mode=access_mode), tmp_path / 'workspace')
    monkeypatch.setitem(runtime_fixture.__globals__, 'SimpleAgent', lambda *_: owned)
    agent, params, task, _, _ = runtime_fixture(tmp_path, monkeypatch)
    assert agent.home_paths.owner_kind == owner_kind
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    snapshot = agent.subagents.runner_context.build_execution_context(child.id)
    from agent_py_agent.agent.subagents.role_templates import DIRECT_CHILD_CONTROL_TOOLS

    assert not set(DIRECT_CHILD_CONTROL_TOOLS) & set(snapshot.allowed_tools)
    assert child.owner == agent.home_paths.owner_id
    assert 'schedule' in snapshot.allowed_tools
    assert agent.tools.runtime_snapshot(allowed_tools=snapshot.allowed_tools).runtime('schedule') is not None
    assert child.attributes['direct_parent_tool_authority']['available_tool_names'] == sorted(params.tool_runtime_snapshot.available_tool_names)
    assert child.attributes['conversation_request_id'] == task.task_id
    assert snapshot.effective_permissions['shell_access_mode'] != 'full-access'
    assert snapshot.effective_permissions['shell_access_mode'] == (
        'restricted' if agent.config.access_mode == 'restricted' else 'workspace-write')
    from agent_py_agent.agent.conversation.host_actor import task_host_actor

    assert task_host_actor(agent, child) == agent._current_run_params.host_channel_actor


@pytest.mark.parametrize('business_tool', ['read_file', 'run_command'])
def test_im_parent_narrow_snapshot_cannot_gain_schedule_from_coding_defaults(tmp_path, monkeypatch, business_tool):
    agent, params, _, _, _ = runtime_fixture(tmp_path, monkeypatch)
    parent_names = ['create_subagents', business_tool]
    params = replace(params, tool_runtime_snapshot=agent.tools.runtime_snapshot(allowed_tools=parent_names))
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    assert 'schedule' not in child.allowed_tools
    assert set(child.allowed_tools) <= {business_tool, 'capability_request', 'record_lesson'}
    assert child.attributes['direct_parent_tool_authority']['available_tool_names'] == parent_names
    assert 'process_session' not in child.allowed_tools  # run_command依赖闭包仍不能越过父快照。


def test_real_group_child_schedule_visibility_does_not_bypass_actor_grant(tmp_path, monkeypatch):
    from agent_py_agent.agent.scheduler.tool import ScheduleTool
    from agent_py_agent.tests.test_group_authorization import (
        grant_synthetic_group_monitor_capability,
    )
    from agent_py_agent.tests.test_host_message_context import select_child
    from agent_py_agent.tests.test_schedule_control import _agent

    owned = _agent(tmp_path, group='oc_a')
    monkeypatch.setitem(runtime_fixture.__globals__, 'SimpleAgent', lambda *_: owned)
    agent, params, _, _, _ = runtime_fixture(tmp_path, monkeypatch, conversation_id='oc_a:thread:future_topic')
    assert agent.home_paths.owner_kind == 'group'
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    assert 'schedule' in agent.subagents.runner_context.build_execution_context(child.id).allowed_tools
    select_child(agent, child)
    raw = {'provider': 'feishu', 'chat_id': 'oc_a', 'app_id': 'app_synthetic', 'tenant_key': 'tenant_synthetic',
        'owner_id': 'ou_fixture_owner', 'member_ids': ['ou_a', 'ou_fixture_owner'],
        'members_complete': True, 'bot_is_member': True}
    adapter = SimpleNamespace(group_authority_snapshot=lambda _: dict(raw))
    agent.delivery_service = SimpleNamespace(registry=SimpleNamespace(adapter_for=lambda _: adapter))
    args = {'action': 'create', 'name': '合成回归', 'prompt': '合成内部检查', 'schedule_kind': 'every', 'every_seconds': 600}
    denied = ScheduleTool(agent).execute(args)
    assert not denied.ok and denied.error_code == 'GROUP_AUTHORIZATION_REQUIRED'
    assert agent.scheduler_repository.list_jobs()[0] == []
    actor = agent._current_run_params.host_channel_actor
    grant_synthetic_group_monitor_capability(agent, actor, members={'ou_a'})
    created = ScheduleTool(agent).execute(args)
    assert created.ok, created.output
    job = json.loads(created.output)['job']
    assert job['creator'] == {'channel': 'feishu', 'actor_id': 'ou_a'}
    assert actor.conversation_id == 'oc_a:thread:future_topic'


def test_manual_child_default_stays_conservative(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

    agent, _, _, _, _ = runtime_fixture(tmp_path, monkeypatch)
    result = CreateSubagentsTool(agent).execute({'goal': '合成手工任务'})
    assert result.ok, result.output
    assert 'schedule' not in agent.subagents.list_runs()[0].allowed_tools


def test_im_child_owner_revocation_keeps_network_and_disabled_dependency_closed(tmp_path, monkeypatch):
    agent, params, _, _, _ = runtime_fixture(tmp_path, monkeypatch)
    agent.subagents.owner_policy_snapshot = {'network': {'enabled': False},
        'tools': {'disabled_tools': ['schedule', 'process_session']}}
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    context = agent.subagents.runner_context.build_execution_context(child.id)
    assert context.effective_permissions['network_enabled'] is False
    assert not {'schedule', 'process_session'} & set(context.allowed_tools)
    assert {'schedule', 'process_session'} <= set(context.effective_permissions['disabled_tools'])


def test_blocked_child_with_open_request_still_enters_direct_parent_resolution(tmp_path, monkeypatch):
    from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams

    agent, params, _, _, _ = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    request = agent.subagents.lifecycle.record_capability_request(child.id, RecordCapabilityRequestParams(
        problem='合成缺能力', needed_capability='合成工具', requested_tools=['schedule']))
    child = agent.subagents.load(child.id)
    child.status = 'BLOCKED'
    child.verification_status = 'UNVERIFIED'
    child.failure_type = 'capability_request'
    agent.subagents.save(child)
    params.live_archive_state.clear()
    resolution = prepare_delegated_execution(agent, params)
    assert resolution is not None and resolution.allowed_tools == ['resolve_capability_requests']
    assert agent.subagents.load(child.id).capability_requests[0].id == request.id
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        pending_natural_user_reply,
    )

    assert pending_natural_user_reply(params) is None
    from agent_py_agent.agent.agent_core import _tool_loop_service as service
    from agent_py_agent.agent.agent_core.orchestration.tools import capability
    from agent_py_agent.agent.backends import ModelResponse

    # LLM: 仅替代模型网络，参数投影及请求采纳仍用正式seam；工具裁决随后走真实Resolve工具，不把模型正文当授权。
    # 函数用途: 验证BLOCKED申请确实能成为有resolve工具的模型请求，而非仍被零工具失败回执抢先。
    def generate(request_params):
        assert request_params.params is resolution
        assert request_params.params.allowed_tools == ['resolve_capability_requests']
        assert request_params.params.run_id == params.run_id and request_params.params.attempt_id == params.attempt_id
        return ModelResponse('合成父级裁决响应', 'fake')

    monkeypatch.setattr(service, 'generate_model_response', generate)
    _, sampled = service._request_tool_loop_model_response(agent, resolution, 0, first_prompt='合成结构化申请上下文')
    assert sampled.backend == 'fake'

    monkeypatch.setattr(capability, '_wake_subagent', lambda *a, **k: None)
    granted = capability.ResolveCapabilityRequestsTool(agent).execute({
        'run_id': child.id, 'request_id': request.id, 'decision': 'grant', 'reason': '合成父级裁决'})
    assert granted.ok, granted.output
    restored = agent.subagents.load(child.id)
    assert restored.status == 'PENDING'
    assert restored.capability_requests[0].status == 'GRANTED'
    assert len(agent.subagents.list_runs()) == 1


@pytest.mark.parametrize('status,request_status', [('BLOCKED', 'GAP'), ('BLOCKED', 'CLOSED'),
                                                  ('FAILED', 'OPEN'), ('CANCELLED', 'OPEN'), ('DONE', 'OPEN')])
def test_terminal_or_gap_children_do_not_reenter_resolution_or_background_wait(tmp_path, monkeypatch, status, request_status):
    from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
        pending_natural_user_reply,
    )
    from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams

    agent, params, _, _, _ = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    agent.subagents.lifecycle.record_capability_request(child.id, RecordCapabilityRequestParams(
        problem='合成申请', needed_capability='合成', requested_tools=['schedule']))
    child = agent.subagents.load(child.id)
    child.status = status
    child.capability_requests[0].status = request_status
    agent.subagents.save(child)
    params.live_archive_state.clear()
    assert prepare_delegated_execution(agent, params) is None
    phase = pending_natural_user_reply(params)
    assert phase['kind'] == ('delegated_result' if status == 'DONE' else 'delegated_failure')
    assert phase['facts']['task_continues_without_more_user_input'] is False


def test_request_replay_uses_saved_route_and_new_disabled_message_is_original(tmp_path, monkeypatch):
    agent, _, task, context, source = runtime_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr("agent_py_agent.agent.gateway_parts.im_task_routing._select_route",
                        lambda *_: pytest.fail("恢复不得再次调用语义路由"))
    assert prepare_im_task_dispatch(context, source, task.goal).task_id == task.task_id
    context.request = {IM_DISPATCH_KEY: {"mode": "original"}}
    assert prepare_im_task_dispatch(context, source, "再聊一句") is None


def test_http_body_cannot_forge_host_dispatch_marker():
    from agent_py_agent.agent.gateway_parts.http_handlers import (
        _AskRequestContext,
        _build_ask_request,
    )

    body = {IM_DISPATCH_KEY: {"mode": "delegated"}, "metadata": {IM_DISPATCH_KEY: {"mode": "delegated"}}}
    request = _build_ask_request(_AskRequestContext(body, "你好", "input", "ou_a", "feishu"))
    assert IM_DISPATCH_KEY not in request


def test_cancel_delegated_task_uses_original_root_stop_and_keeps_chat(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.tools.session_task_control import (
        CancelSessionTaskTool,
    )

    agent, params, task, _, source = runtime_fixture(tmp_path, monkeypatch)
    prepare_delegated_execution(agent, params)
    fixed = agent.conversation_store.threads.require(task.target_thread_id).metadata[IM_TASK_EXECUTION_KEY]
    outcome = CancelSessionTaskTool(agent).execute({"task_id": task.task_id})
    assert outcome.ok, outcome.output
    payload = json.loads(outcome.output)
    assert payload["stop_confirmed"], payload
    assert agent.conversation_store.tasks.load(fixed["root_task_id"]).status == "interrupted"
    assert agent.conversation_store.session_tasks.load(task.task_id).status == "cancelled"
    assert all(agent.subagents.load(run_id).status == "CANCELLED" for run_id in fixed["child_run_ids"])
    assert agent.conversation_store.threads.require(source.thread_id).status == "active"
