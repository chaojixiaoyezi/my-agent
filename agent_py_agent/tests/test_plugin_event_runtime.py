"""B4 生产工具入口的宿主身份传递，不直接构造 ToolExecutorRequest。"""
from __future__ import annotations

from dataclasses import replace
from functools import partial
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
from agent_py_agent.tests.test_tool_operation_managed_gate import (
    _direct_register_chain,
    _gate_call,
    _managed_chain,
    _repo,
    _store,
)
from agent_py_agent.tests.test_tool_operation_managed_gate import (
    _params as _managed_params,
)
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


class _UnreadableConfigAgent(SimpleNamespace):
    @property
    def config(self):
        raise RuntimeError('config read failed')


class _UnreadableEventSwitch:
    @property
    def plugin_events_enabled(self):
        raise RuntimeError('event switch read failed')


# LLM: 故障仅限可选观察配置视图，执行器/权限/核验仍使用原 Agent；复用真实装配函数，不伪造返回结果。
# 函数用途: 给装配边界注入 getter 故障，并将原请求原样交回正式观察函数。
def _assemble_with_config_view(view, request, scope):
    assemble, agent = view
    return assemble(replace(request, agent=agent), scope)


@pytest.mark.parametrize('fault', ['missing_config', 'config_read', 'switch_read', 'disabled'])
def test_optional_event_config_fault_never_aborts_real_tool(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.agent_core import tool_call_runtime
    from agent_py_agent.agent.gateway_parts import plugin_panels_http

    repo, store, tool = _repo(tmp_path), _store(tmp_path), _CountingTool()
    _agent_run_id, attempt_id = _direct_register_chain(repo, 'event-run')
    agent = _managed_chain(repo, store, [tool], tmp_path)
    configs = {'switch_read': _UnreadableEventSwitch(), 'disabled': SimpleNamespace(plugin_events_enabled=False)}
    if fault in configs:
        agent.config = configs[fault]
    if fault == 'config_read':
        # 故障只注入可选观察装配；原 write_boundary 的配置读取属于权限链，不能吞掉它的失败。
        original_context = tool_call_runtime._tool_event_context
        broken_agent = _UnreadableConfigAgent(**vars(agent))
        monkeypatch.setattr(tool_call_runtime, '_tool_event_context',
                            partial(_assemble_with_config_view, (original_context, broken_agent)))
    events = []
    monkeypatch.setattr(plugin_panels_http, 'publish_plugin_event', lambda *args: events.append(args))
    snapshot = _snapshot(tool, run_id='event-run')
    params = replace(_managed_params('event-run', 'task-event-run', snapshot=snapshot), attempt_id=attempt_id)
    call = _gate_call(run_id='event-run', tool_name=tool.model_spec.name, snapshot=snapshot,
                      arguments={'value': 'hello'}, attempt_id=attempt_id)
    execution = execute_traced_tool_call(ToolCallRuntimeRequest(agent, ToolCallExecuteParams(params, 1, 1, call), call))
    assert execution.result.ok, execution.result.error_code
    assert execution.result.handler_executed is True
    assert tool.executions == 1
    assert events == []


@pytest.mark.parametrize('fault', ['routing_read', 'assembler'])
def test_optional_event_assembly_fault_never_aborts_real_tool(gateway, monkeypatch, fault):
    from agent_py_agent.agent.gateway_parts import event_points

    class Routing(dict):
        def get(self, key, default=None):
            if key == 'plugin_event_channel':
                raise RuntimeError('routing read failed')
            return super().get(key, default)

    agent, _paths, _server, events = gateway
    tool = _CountingTool()
    params = _params(agent, tool)
    if fault == 'routing_read':
        params = replace(params, task_attributes=Routing(params.task_attributes))
    else:
        def broken_assembly(*_args, **_kwargs):
            raise RuntimeError('event assembly failed')
        monkeypatch.setattr(event_points, 'gateway_event_context', broken_assembly)
    call = ToolCall(call_id='event-call', tool_name=tool.model_spec.name, arguments={'value': 'hello'},
                    source_protocol='native', schema_hash=tool.model_spec.schema_hash,
                    run_id=params.run_id, turn_id='event-turn', attempt_id=params.attempt_id)
    execution = execute_traced_tool_call(ToolCallRuntimeRequest(agent, ToolCallExecuteParams(params, 1, 1, call), call))
    assert execution.result.ok, execution.result.error_code
    assert execution.result.handler_executed is True
    assert tool.executions == 1
    assert events == []


def test_event_switch_off_never_reads_optional_routing_or_assembler(monkeypatch):
    from agent_py_agent.agent.agent_core.tool_call_runtime import _tool_event_context

    class Request:
        @property
        def params(self):
            pytest.fail('disabled observation must not read routing')

    agent = SimpleNamespace(config=SimpleNamespace(plugin_events_enabled=False))
    assert _tool_event_context(SimpleNamespace(agent=agent, request=Request()), object()) is None


def test_registry_optional_event_context_read_failure_does_not_abort_real_handler(gateway):
    class Context(dict):
        def get(self, key, default=None):
            if key == 'plugin_event_context':
                raise RuntimeError('context projection failed')
            return super().get(key, default)

    from agent_py_agent.agent.agent_core.tool_loop.recovery import runtime_run_scope

    agent, _paths, _server, events = gateway
    tool = _CountingTool()
    params = _params(agent, tool)
    call = ToolCall(call_id='registry-event-call', tool_name=tool.model_spec.name, arguments={'value': 'hello'},
                    source_protocol='native', schema_hash=tool.model_spec.schema_hash,
                    run_id=params.run_id, turn_id='event-turn', attempt_id=params.attempt_id)
    execution = agent.tools.execute_tool(call, write_boundary={}, runtime_snapshot=params.tool_runtime_snapshot,
        trusted_run_context=Context(run_scope=runtime_run_scope(agent, params).to_dict()))
    assert execution.result.ok, execution.result.error_code
    assert execution.result.handler_executed is True
    assert tool.executions == 1
    assert events == []
