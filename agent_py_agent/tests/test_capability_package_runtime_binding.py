"""能力包首次使用沿原任务晋升；全部使用临时原组件，不启动模型或外部服务。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallExecuteParams
from agent_py_agent.agent.capability import SkillSnapshotError
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.contracts.tool_manifest_contract import tool_manifest_payload
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
from agent_py_agent.agent.gateway_parts.request_context import (
    GatewayAskRunContext,
    GatewayConversationContext,
)
from agent_py_agent.agent.gateway_parts.request_errors import ConversationTaskBindingError
from agent_py_agent.agent.gateway_parts.request_execution import (
    _gateway_run_params,
    _GatewayRunParamsRequest,
)
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.tooling.models import (
    ToolParameterCondition,
    ToolRuntime,
    tool_promotes_task,
)
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests._tool_runtime_harness import canonical_test_call
from agent_py_agent.tests.test_capability_package_task_refs import _agent


# LLM: 当前输入仅建立真实线程，首次工作动作仍由生产晋升入口创建任务；测试不预造应由工具创建的 pins。
# 函数用途: 构造尚未晋升的会话和工具循环参数。
def _unpromoted_turn(agent):
    thread = agent.conversation_store.threads.get_or_create({"canonical_user_id": "local/main", "channel": "cli", "channel_conversation_id": "unpromoted-package"})
    snapshot = agent.tools.runtime_snapshot(run_id="first-package-read")
    params = ToolLoopExecuteParams(
        user_prompt="用故事能力包整理内容", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[],
        effective_on_chunk=None, allowed_tools=None, write_boundary=None,
        task_attributes={"conversation_thread_id": thread.thread_id}, request_id="first-package-read",
        run_id="first-package-read", task_id="first-package-read", one_shot_tool_calls=set(),
        executed_tools=[], archive_tool_calls=[], root_user_prompt="用故事能力包整理内容",
        context_scope="conversation", tool_runtime_snapshot=snapshot,
    )
    return thread, params


# LLM: 调用真实工具执行缝及 Registry；不绕过晋升、正文校验或 pin；managed-operation 授权由原专门测试负责。
# 函数用途: 在本地组件测试中执行一次包发现或读取并保留原任务事实。
def _execute(agent, params, arguments, call_id):
    call = canonical_test_call(params.tool_runtime_snapshot, "skill_search", arguments, call_id=call_id)
    agent.tools.operation_store = None
    agent.tools.operation_store_required = False
    agent._current_run_params = params
    return execute_traced_tool_call(ToolCallRuntimeRequest(agent, ToolCallExecuteParams(params, 1, 1, call), call)).result


def test_first_package_get_promotes_before_pin_but_search_remains_taskless(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import task_references

    agent, _store, _entries = _agent(tmp_path)
    thread, params = _unpromoted_turn(agent)
    searched = _execute(agent, params, {"action": "search", "package_id": "story-a"}, "search-package")
    assert searched.ok, searched.output
    assert agent.conversation_store.tasks.list(thread.thread_id) == []
    assert "conversation_task_id" not in params.task_attributes
    original = task_references.pin_package_reference
    seen = []

    def observe_pin(current, reference):
        task_id = current._current_run_params.task_attributes["conversation_task_id"]
        assert current.conversation_store.tasks.load(task_id).thread_id == thread.thread_id
        seen.append(task_id)
        original(current, reference)

    monkeypatch.setattr(task_references, "pin_package_reference", observe_pin)
    read = _execute(agent, params, {"action": "get", "package_id": "story-a"}, "get-package")
    assert read.ok, read.output
    task = agent.conversation_store.tasks.load(params.task_attributes["conversation_task_id"])
    assert seen == [task.task_id]
    assert task.skill_snapshot_refs[0]["package_id"] == "story-a"


@pytest.mark.parametrize("arguments,expected", [
    ({"action": "get", "package_id": "story-a"}, True),
    ({"action": "search", "package_id": "story-a"}, False),
    ({"action": "get", "skill_id": "builtin:ordinary"}, False),
    ({"action": "get", "package_id": " "}, False),
    ({"action": "get", "package_id": True}, False),
    ({"package_id": "story-a"}, False),
])
def test_task_promotion_conditions_use_only_typed_arguments(arguments, expected):
    policy = SkillSearchTool.runtime_policy
    assert tool_promotes_task(policy, arguments) is expected
    assert tool_promotes_task(replace(policy, promotes_task=True), arguments)
    assert not tool_promotes_task(replace(policy, promotes_task_when=()), arguments)


def test_task_promotion_conditions_are_validated_and_projected_from_same_schema(tmp_path):
    agent, _store, _entries = _agent(tmp_path)
    snapshot = agent.tools.runtime_snapshot(run_id="manifest-package")
    row = next(item for item in tool_manifest_payload(snapshot)["tools"] if item["name"] == "skill_search")
    assert row["runtime_policy"]["promotes_task_when"] == [
        {"field": "action", "operator": "equals", "value": "get"},
        {"field": "package_id", "operator": "nonempty_string", "value": ""},
    ]
    tool = SkillSearchTool(agent)
    for condition in (ToolParameterCondition("undeclared", "nonempty_string"),
                      ToolParameterCondition("action", "equals", "unknown"),
                      ToolParameterCondition("offset", "nonempty_string")):
        with pytest.raises(ValueError):
            ToolRuntime(tool.model_spec, replace(tool.runtime_policy, promotes_task_when=(condition,)), tool)
    with pytest.raises(ValueError):
        replace(tool.runtime_policy, promotes_task_when=[ToolParameterCondition("action", "equals", "get")])


# LLM: 原Gateway参数装配保留完整thread与原Goal事实；空目录不构造替代工作区。
# 函数用途: 为已有但尚未物化目录的任务重建一轮真实入口参数。
def _gateway_params(agent, tmp_path, thread, goal, *, request=None):
    request = request or {"id": "followup", "prompt": "继续当前任务"}
    conversation = GatewayConversationContext(thread_id=thread.thread_id, thread_goal=goal, compact_generation=2)
    context = GatewayAskRunContext(agent, request, tmp_path / "request.json", tmp_path / "response.json", request["id"], None)
    return _gateway_run_params(_GatewayRunParamsRequest(request, context, conversation, request["prompt"]))


def test_goal_without_directory_keeps_pins_in_gateway_rebuild_and_background(tmp_path):
    agent, store, entries = _agent(tmp_path)
    thread, _params = _unpromoted_turn(agent)
    goal = agent.conversation_store.goals.create({"thread_id": thread.thread_id, "task_id": "goal-without-directory", "objective": "整理内容"})
    task = agent.conversation_store.tasks.bind({"thread_id": thread.thread_id, "task_id": goal.task_id,
                                              "goal": goal.objective, "status": "active"})
    assert task.task_path == ""
    reference = agent.current_skill_snapshot().resolve_package("story-a").to_ref()
    agent.conversation_store.tasks.pin_skill_reference(task_id=task.task_id, thread_id=thread.thread_id, reference=reference)
    params = _gateway_params(agent, tmp_path, thread, goal.to_dict())
    assert params.task_attributes["conversation_task_id"] == task.task_id
    assert agent.conversation_store.tasks.load(task.task_id).task_path == ""
    background = _run_params(thread.thread_id, BackgroundRunRequest(thread.thread_id, task.task_id), agent, thread=thread)
    for current in (params, background):
        agent._current_run_params = current
        assert agent.current_skill_snapshot().resolve_package("story-a").to_ref() == reference
    active = entries[0]
    revoked = store.change_activation(PluginActivationRequest("disable-goal", active.revision, replace(active.activation, phase="revoked"))).installation
    released, _ = store.release_activation(resolve_owner_home(agent.home_paths.root), None, revoked, "disable-goal")
    current = released.installation
    new = PluginContentActivation("reenable-goal", current.manifest.plugin_id, current.package_sha256, current.revision, current.settings_revision)
    store.change_activation(PluginActivationRequest("reenable-goal", current.revision, new))
    for current in (params, background):
        agent._current_run_params = current
        snapshot = agent.skill_snapshot_for_run_scope(agent.effective_workspace_root)
        assert snapshot.resolve_package("story-a") is None
        assert snapshot.resolve_package("story-b") is not None
        assert any(error.code == "CAPABILITY_PACKAGE_PIN_STALE" for error in snapshot.errors)
    assert agent.conversation_store.tasks.load(task.task_id).skill_snapshot_refs == (reference,)


def test_gateway_binding_rejects_foreign_goal_and_keeps_unpromoted_runtime_taskless(tmp_path):
    agent, _store, _entries = _agent(tmp_path)
    thread, _ = _unpromoted_turn(agent)
    other = agent.conversation_store.threads.get_or_create({"canonical_user_id": "local/main", "channel": "cli", "channel_conversation_id": "other"})
    agent.conversation_store.tasks.bind({"thread_id": other.thread_id, "task_id": "foreign-task", "goal": "其它任务"})
    with pytest.raises(ConversationTaskBindingError):
        _gateway_params(agent, tmp_path, thread, {"task_id": "foreign-task", "status": "active"})
    with pytest.raises(ConversationTaskBindingError):
        _gateway_params(agent, tmp_path, thread, {"task_id": "missing-task", "status": "active"})
    request = {"id": "runtime-only", "prompt": "继续普通聊天", "execution_attempt_id": "gateway-attempt",
               "runtime_authority": {"schema_version": "gateway_runtime_authority.v1", "request_id": "runtime-only",
                                     "gateway_execution_attempt_id": "gateway-attempt", "task_id": "runtime-only-task",
                                     "run_id": "runtime-only-run", "agent_run_id": "runtime-only-agent", "attempt_id": "attempt"}}
    params = _gateway_params(agent, tmp_path, thread, None, request=request)
    assert params.task_id == "runtime-only-task" and "conversation_task_id" not in params.task_attributes
    agent._current_run_params = params
    assert agent.current_skill_snapshot().packages


def test_same_thread_wrong_task_payload_cannot_supply_another_tasks_pins(tmp_path):
    agent, _store, _entries = _agent(tmp_path)
    thread, _params = _unpromoted_turn(agent)
    tasks = agent.conversation_store.tasks
    first = tasks.bind({"thread_id": thread.thread_id, "task_id": "first-task", "goal": "第一个任务"})
    second = tasks.bind({"thread_id": thread.thread_id, "task_id": "second-task", "goal": "第二个任务"})
    reference = agent.current_skill_snapshot().packages[0].to_ref()
    tasks.pin_skill_reference(task_id=first.task_id, thread_id=thread.thread_id, reference=reference)
    path = agent.conversation_store.storage.task_path(first.task_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["task_id"] = second.task_id
    path.write_text(json.dumps(payload), encoding="utf-8")
    link, error = tasks.load_report(first.task_id)
    assert link is None and error is not None and error["task_id"] == first.task_id
    with pytest.raises(ConversationTaskBindingError):
        _gateway_params(agent, tmp_path, thread, {"task_id": first.task_id, "status": "active"})
    agent._current_run_params = RunParams(task_attributes={"conversation_thread_id": thread.thread_id,
                                                        "conversation_task_id": first.task_id})
    with pytest.raises(SkillSnapshotError, match="BINDING_INVALID"):
        agent.current_skill_snapshot()
    assert tasks.load(second.task_id) == second
