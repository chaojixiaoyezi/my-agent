"""13 个请求、1/12 个直属孩子与完整 32/64 对工具 IR 的真实物化/适配合同。

这里不调用真实模型或派生代理：只替换宿主来源，仍用正式 builder、冻结投影、IR 物化和 provider adapter。
"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._tool_loop_service import (
    _runtime_injections_with_delivery_contract,
)
from agent_py_agent.agent.agent_core.runtime.conversation_state import (
    conversation_runtime_state_section,
)
from agent_py_agent.agent.agent_core.runtime.guidance import (
    observed_direct_children,
    refresh_runtime_direct_children_snapshot,
)
from agent_py_agent.agent.agent_core.tool_ir_history import (
    open_assistant_turn_ir,
    record_tool_call_ir,
)
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    _materialize_native_prompt_facts,
    _native_provider_messages,
)
from agent_py_agent.agent.agent_core.tool_request_projection import (
    ToolLoopRequestInput,
    project_tool_loop_request,
    tool_loop_prompt_request,
)
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, RuntimeFactsTurn, ToolResult
from agent_py_agent.agent.prompting_parts.builder import PromptBuilder, render_prepared_prompt
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.prompting_parts.runtime_injection import (
    RUNTIME_DIRECT_CHILDREN_SOURCE,
    RUNTIME_GOAL_SOURCE,
    RuntimeInjectionFragment,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
)
from agent_py_agent.tests.test_runtime_guidance import (
    _direct_child,
    _MutableSubagentManager,
    _tool_loop_params,
)


@pytest.mark.parametrize('children', [1, 12])
@pytest.mark.parametrize('batch', [32, 64])
def test_thirteen_frozen_requests_match_real_materialization_and_keep_visible_prefix(tmp_path, monkeypatch, children, batch):
    from agent_py_agent.agent.conversation import goal_prompting

    goal, goal_reads = ['goal A'], []
    monkeypatch.setattr(goal_prompting, 'current_goal_scope_prompt', lambda *_: goal_reads.append(True) or goal[0])
    manager = _MutableSubagentManager([_direct_child(f'child-{i}', 'root', 'RUNNING') for i in range(children)])
    config = AgentConfig(prompt_files=[], home_context_enabled=False, assistant_progress_updates_enabled=False)
    agent = SimpleNamespace(config=config, subagents=manager, backend=SimpleNamespace(name='echo'))
    builder = PromptBuilder(config, tmp_path)
    raw_marker = '[RUNTIME_DIRECT_CHILDREN]\nopaque user text'
    params = _tool_loop_params(task_id='root', runtime_injections=['base\n', '', raw_marker])
    previous_messages, stable, submitted = [], None, []
    for turn in range(13):
        if turn == 3:
            manager.runs[-1].status = 'DONE'
        if turn == 5:
            manager.load_errors = [{'run_id': 'partial'}]
        if turn == 7:
            manager.runs, manager.load_errors = [], []
        if turn == 9:
            manager.runs = [_direct_child('late-child', 'root', 'CANCELLED')]
        goal[0] = None if turn in (6, 7) else ('goal B' if turn == 8 else 'goal A')
        refresh_runtime_direct_children_snapshot(agent, params)
        assert raw_marker in params.runtime_injections
        fragments = _runtime_injections_with_delivery_contract(params, agent=agent)
        frozen = builder.prepare_render_input(tool_loop_prompt_request(params, runtime_injections=fragments,
            workspace_context='frozen workspace', execution_facts='fixed evidence'))
        request = ToolLoopRequestInput(prompt_input=frozen, system_instruction='system',
            tool_protocol_snapshot=params.tool_protocol_snapshot, tool_choice=ToolChoice(mode='auto'), native_tools=(),
            tool_ir_history=tuple(params.tool_ir_history), provider_history_messages=(), tool_context=(),
            forwarded_guidance=frozenset(), conversation_state=conversation_runtime_state_section(params))
        original = list(params.tool_ir_history)
        projected = project_tool_loop_request(request)
        assert all(a is b for a, b in zip(params.tool_ir_history, original)) and len(params.tool_ir_history) == len(original)
        # 候选档案复制和后续配置修改不能重采集 Goal/孩子，也不能改变同次开关。
        assert project_tool_loop_request(deepcopy(request)) == projected
        assert goal_reads == [True] * (turn + 1)
        prompt = render_prepared_prompt(frozen)
        layout = prompt_cache_layout(prompt)
        stable = layout.stable_prefix if stable is None else stable
        assert layout.stable_prefix == stable
        materialized = _materialize_native_prompt_facts(ModelGenerateParams(agent, params, prompt, turn))
        assert materialized.prompt == projected.provider_prompt
        messages = _native_provider_messages(agent, params)
        assert messages == projected.messages
        assert messages[:len(previous_messages)] == previous_messages
        assert 'RuntimeInjectionFragment(' not in str(messages)
        assert sum(isinstance(value, RuntimeInjectionFragment) and value.source == RUNTIME_DIRECT_CHILDREN_SOURCE
                   for value in params.runtime_injections) == 1
        if turn == 7:
            assert observed_direct_children(params) == {}
            assert any(isinstance(item, RuntimeFactsTurn) and item.source == RUNTIME_GOAL_SOURCE
                       and 'runtime-injection-cleared' in item.text for item in params.tool_ir_history)
        submitted.append(projected)
        previous_messages = deepcopy(messages)
        # 不缩批次，保留每个调用/回执的独立身份和真实配对顺序。
        open_assistant_turn_ir(params, tool_rounds=turn + 1, response_text=f'round-{turn}')
        for index in range(batch):
            call = canonical_history_call('read_file', {'path': f'file-{turn}-{index}'},
                call_id=f'call-{turn}-{index}', run_id=params.run_id, attempt_id='attempt', turn_id=f'turn-{turn}')
            record_tool_call_ir(params, tool_rounds=turn + 1, call=call, result=canonical_history_result(call, f'body-{turn}-{index}'))
    assert len(submitted) == 13
    assert sum(isinstance(item, ToolResult) for item in params.tool_ir_history) == 13 * batch
    assert [len(item.tool_calls) for item in params.tool_ir_history if isinstance(item, AssistantTurn)] == [batch] * 13


def test_cancelled_unsubmitted_projection_does_not_advance_surviving_baseline():
    from agent_py_agent.tests.test_cache_opt_runtime_sources import project
    params = _tool_loop_params()
    from agent_py_agent.agent.agent_core.tool_ir_history import project_native_prompt_history
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt

    prompt = CacheStructuredPrompt('stable', volatile_sections=((RUNTIME_GOAL_SOURCE, 'candidate'),))
    _, candidate = project_native_prompt_history(params, prompt, conversation_state='')
    assert candidate and params.tool_ir_history == []
    _, reloaded = project([], [(RUNTIME_GOAL_SOURCE, 'candidate')])
    assert reloaded == candidate
    assert replace(_tool_loop_params(), tool_ir_history=candidate).tool_ir_history is candidate


def test_subagent_first_request_refresh_keeps_typed_slots_and_same_frozen_switch(monkeypatch):
    from agent_py_agent.agent.agent_core.subagent import model_selection
    from agent_py_agent.agent.prompting_parts.builder import PromptBuildRequest
    from agent_py_agent.tests.test_cache_opt_snapshot_contract import prepared

    values = ['raw base', RuntimeInjectionFragment(None, RUNTIME_DIRECT_CHILDREN_SOURCE),
              RuntimeInjectionFragment('full goal', RUNTIME_GOAL_SOURCE)]
    prompt_input = prepared(values, enabled=False)
    request_input = ToolLoopRequestInput(prompt_input=prompt_input, tool_ir_history=())
    preparation = model_selection.SubagentFirstRequestPreparation(object(), 'run', 'attempt',
        prompt_request=PromptBuildRequest('question', [], inject=['old']))
    monkeypatch.setattr(model_selection, 'first_request_preparation', lambda *_: preparation)
    model_selection.refresh_first_request_prompt(object(), object(), request_input)
    assert preparation.prompt_request.inject == values
    assert preparation.prompt_request.inject is not values
    assert preparation.prompt_input is prompt_input
    assert deepcopy(preparation.prompt_input).injection_fragments == tuple(values)
    assert not replace(preparation.prompt_input).prompt_runtime_snapshots_enabled
