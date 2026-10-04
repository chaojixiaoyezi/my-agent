"""B4 生产工具入口的宿主身份传递，不直接构造 ToolExecutorRequest。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallExecuteParams
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.tests.test_plugin_event_gateway import gateway
from agent_py_agent.tests.test_tool_runtime_scope import _register_run
from agent_py_agent.tests.test_tool_runtime_unification import _CountingTool, _snapshot

_FIXTURES = (gateway,)


def _params(agent, tool, *, kind='main'):
    run_id = 'event-run'
    if kind == 'subagent':
        parent = agent.subagents.create_run(goal='event parent', thought='test', plan=['child'])
        child = agent.subagents.create_run(goal='event child', thought='test', plan=['echo'],
                                          parent_id=parent.id, root_id=parent.id, depth=1)
        run_id = child.id
        repo = agent.subagents.runtime_db
        row = repo.agent_run_for_run_id(run_id)
        repo.create_attempt(row['agent_run_id'], reuse_pending=True)
    attempt_id = _register_run(agent, run_id)
    task_id = agent.subagents.runtime_db.task_id_for_run_id(run_id)
    return ToolLoopExecuteParams(
        user_prompt='event-test', memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section='', tool_recommendations_section='', tool_context=[], effective_on_chunk=None,
        allowed_tools=[tool.model_spec.name], write_boundary=None,
        task_attributes={'conversation_thread_id': 'event-thread'},
        request_id='event-request', run_id=run_id, task_id=task_id, attempt_id=attempt_id, save=False,
        one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[],
        tool_runtime_snapshot=_snapshot(tool, run_id=run_id),
        context_scope='task_local' if kind == 'subagent' else 'default',
    )


@pytest.mark.parametrize('kind', ['main', 'subagent'])
def test_real_runtime_registry_to_handler_uses_host_scope(gateway, kind):
    agent, _paths, _server, events = gateway
    tool = _CountingTool()
    params = _params(agent, tool, kind=kind)
    call = ToolCall(call_id='event-call', tool_name=tool.model_spec.name, arguments={'value': 'token=private-marker'},
                    source_protocol='native', schema_hash=tool.model_spec.schema_hash,
                    run_id=params.run_id, turn_id='event-turn', attempt_id=params.attempt_id)
    request = ToolCallExecuteParams(params, 1, 1, call)
    execution = execute_traced_tool_call(ToolCallRuntimeRequest(agent, request, call))
    assert execution.result.ok, (execution.result.error_code, execution.result.output, execution.result.metadata)
    assert tool.executions == 1
    assert [e.type for _, e in events] == ['tool_call_started', 'tool_call_finished']
    assert {e.actor for _, e in events} == {kind}
    assert all('private-marker' not in str(e.facts) and not e.content for _, e in events)


def test_j16_host_action_passes_decision_identity_to_real_runtime(gateway):
    from agent_py_agent.agent.agent_core.tool_context.decision_action_execute import (
        AutoExecutionSelection,
        HostAction,
    )
    from agent_py_agent.agent.agent_core.tool_loop import round_execution
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolRoundExecutionRequest
    from agent_py_agent.agent.backends import ModelResponse

    agent, _paths, _server, events = gateway
    tool = _CountingTool()
    params = _params(agent, tool)
    call = ToolCall(call_id='host-call', tool_name=tool.model_spec.name, arguments={'value': 'private-marker'},
                    source_protocol='native', schema_hash=tool.model_spec.schema_hash,
                    run_id='event-run', turn_id='event-turn', attempt_id=params.attempt_id)
    def execute(request):
        return execute_traced_tool_call(ToolCallRuntimeRequest(agent, request, request.call))
    request = ToolRoundExecutionRequest(agent, params, 1, ModelResponse(text='', backend='test'), [],
                                        execute, lambda *_: None)
    # 观察/决策选择边界使用已计划动作；真实宿主执行、Registry、执行器与 handler 不替换。
    selection = AutoExecutionSelection('action_candidate', SimpleNamespace(operation_id='event-op'),
        SimpleNamespace(response=SimpleNamespace(input_digest='event-digest')),
        {'observation_id': 'event-observation'}, {'candidate_id': 'event-candidate'})
    action = HostAction(call=call, selection=selection)
    round_execution._execute_host_action(request, 1, action)
    assert tool.executions == 1
    assert [e.type for _, e in events] == ['tool_call_started', 'tool_call_finished']
    assert {e.actor for _, e in events} == {'decision'}
