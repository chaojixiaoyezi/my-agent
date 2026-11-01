"""W5 的业务材料/恢复合同与真实 Compact 入口；W2 canonical 业务恢复另行联验。"""

import pytest

from agent_py_agent.agent.agent_core import compact_request_recovery as recovery
from agent_py_agent.agent.agent_core.runtime.loop_support import _replayable_turn_history
from agent_py_agent.agent.agent_core.runtime.turn_trigger import TurnTrigger
from agent_py_agent.agent.agent_core.tool_request_projection import ToolLoopRequestInput
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.conversation import background_execution, goal_prompting
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.prompting_parts.runtime_injection import (
    RUNTIME_DIRECT_CHILDREN_SOURCE,
    RUNTIME_GOAL_SOURCE,
    RUNTIME_INJECTION_SOURCE,
    RuntimeInjectionFragment,
    runtime_injection_clear_text,
)
from agent_py_agent.tests.test_cache_opt_snapshot_contract import prepared
from agent_py_agent.tests.test_compact_native_ir_recovery import _native_ir_attempt
from agent_py_agent.tests.test_gateway_compact_recovery import _history_request
from agent_py_agent.tests.test_gateway_model_adoption import fake_http
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params
from agent_py_agent.tests.test_subagent_compact_recovery import _http


def business_context():
    return BusinessExecutionContext(owner_id='owner', space_id='space-a', project_ref='project-a', task_id='stask-original',
        space_revision=7, binding_id='binding', selected_method_refs=('method:one',),
        asset_version_refs=('asset:one@3',), schema_version='business-execution-context.v1')


def test_replace_keeps_original_slots_sources_frozen_bool_and_business_snapshot():
    business = business_context().to_dict()
    values = ['foreground recovery stays raw', RuntimeInjectionFragment('old', RUNTIME_GOAL_SOURCE),
              RuntimeInjectionFragment(None, RUNTIME_DIRECT_CHILDREN_SOURCE)]
    params = _tool_loop_params(runtime_injections=values, task_attributes={'business_context': business})
    frozen = ToolLoopRequestInput(prompt_input=prepared(values, enabled=False), tool_ir_history=())
    candidate, output = recovery.replace_recovery_history(params, frozen, history_seed=None,
        injection='replacement', injection_index=1, compact_context=None)
    assert candidate.runtime_injections == ['foreground recovery stays raw',
        RuntimeInjectionFragment('replacement', RUNTIME_GOAL_SOURCE), values[2]]
    assert output.prompt_input.injection_fragments == tuple(candidate.runtime_injections)
    assert not output.prompt_input.prompt_runtime_snapshots_enabled
    assert candidate.tool_context is params.tool_context
    assert candidate.task_attributes['business_context'] == business
    assert business['task_id'] != candidate.task_id
    assert params.runtime_injections == values and frozen.prompt_input.injection_fragments == tuple(values)


@pytest.mark.parametrize('index', [-1, 3])
def test_unknown_recovery_slot_does_not_mutate_original(index):
    from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
    params = _tool_loop_params(runtime_injections=['base'])
    frozen = ToolLoopRequestInput(prompt_input=prepared(('base',)), tool_ir_history=())
    with pytest.raises(ConversationCompactError) as failure:
        recovery.replace_recovery_history(params, frozen, history_seed=None, injection='new', injection_index=index, compact_context=None)
    assert failure.value.code == 'COMPACT_REQUEST_PROJECTION_UNKNOWN'
    assert params.runtime_injections == ['base']


def test_lifecycle_filter_uses_structural_family_and_keeps_real_user_event_order():
    text = runtime_injection_clear_text(RUNTIME_GOAL_SOURCE)
    user = UserTurn(text)
    other = RuntimeFactsTurn(text, source='runtime.guidance')
    prefix_collision = RuntimeFactsTurn(text, source=RUNTIME_INJECTION_SOURCE + 'X')
    raw = RuntimeFactsTurn('base', source=RUNTIME_INJECTION_SOURCE)
    goal = RuntimeFactsTurn(text, source=RUNTIME_GOAL_SOURCE)
    future = RuntimeFactsTurn('future', source=RUNTIME_INJECTION_SOURCE + '.future')
    history = [raw, user, other, goal, prefix_collision, future]
    assert _replayable_turn_history(history, None) is history
    result = _replayable_turn_history(history, TurnTrigger(kind='lifecycle_wake'))
    assert result == [user, other, prefix_collision]
    assert result[0] is user and result[1] is other
    assert history == [raw, user, other, goal, prefix_collision, future]


def test_foreground_real_gateway_compact_keeps_raw_recovery_and_frozen_switch(tmp_path, monkeypatch):
    from agent_py_agent.agent import gateway_compact_recovery
    from agent_py_agent.agent.gateway_parts import request_execution
    fixture = _history_request(tmp_path)
    candidates, sent = [], []
    original = recovery.replace_recovery_history

    def observe(*args, **kwargs):
        value = original(*args, **kwargs)
        candidates.append(value)
        return value

    def business(wire):
        if not sent:
            sent.append('overflow')
            raise ProviderContextWindowError('test overflow')
        sent.append('request')

    monkeypatch.setattr(gateway_compact_recovery, 'replace_recovery_history', observe)
    fake_http(monkeypatch, fixture, on_business=business)
    request_execution._run_gateway_ask(fixture.context)
    assert candidates and sent[0] == 'overflow'
    for params, frozen in candidates:
        assert frozen.prompt_input.prompt_runtime_snapshots_enabled is True
        assert frozen.prompt_input.injection_fragments[:3] == tuple(fixture.context.request['inject'])
        assert params.runtime_injections[:3] == fixture.context.request['inject']
        assert all(isinstance(item, str) for item in params.runtime_injections[:3])
    assert fixture.agent.conversation_store.threads.require(fixture.thread_id).compact_generation == 1


@pytest.mark.parametrize('backend', ['anthropic_compatible', 'openai_compatible'])
def test_background_real_recovery_preserves_business_material_and_each_full_snapshot(tmp_path, monkeypatch, backend):
    agent, store, thread, execution, sink, history, params, context, _recorded = _native_ir_attempt(
        tmp_path, monkeypatch, backend=backend, full_result='FULL-BODY-' + 'x' * 5000)
    business = business_context().to_dict()
    params.task_attributes['business_context'] = business
    goal_reads = []
    monkeypatch.setattr(goal_prompting, 'current_goal_scope_prompt', lambda *_: goal_reads.append(True) or 'complete frozen goal')
    candidates = []
    original = recovery._project_mixed_recovery_material

    def observe(*args):
        value = original(*args)
        if args[1].is_candidate:
            candidates.append(value)
        return value

    monkeypatch.setattr(recovery, '_project_mixed_recovery_material', observe)
    _http(monkeypatch, backend=backend, on_business=lambda *_: None)
    result, _, host = background_execution._run_background_recovery_attempt(execution, '继续核对', params, history, context,
        recovering=True, activity_sink=sink)
    assert host.committed and result.runtime_status != 'context_overflow'
    assert goal_reads == [True]
    assert candidates
    candidate = candidates[-1]
    assert candidate.params.task_attributes['business_context'] == business
    assert candidate.params.task_attributes['business_context']['task_id'] == 'stask-original'
    assert isinstance(candidate.params.runtime_injections[0], str)
    fragments = candidate.request_input.prompt_input.injection_fragments
    assert [value for value in fragments if isinstance(value, RuntimeInjectionFragment) and value.source == RUNTIME_GOAL_SOURCE] == [
        RuntimeInjectionFragment('complete frozen goal', RUNTIME_GOAL_SOURCE)]
    assert store.threads.require(thread.thread_id).compact_generation == 1


def test_original_foreground_recovery_notice_stays_raw_base_when_goal_is_withdrawn():
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.agent_core.runtime_mixin import _foreground_recovery_params
    from agent_py_agent.agent.prompting_parts.builder import render_prepared_prompt
    from agent_py_agent.tests.test_cache_opt_runtime_sources import project

    original = RunParams(inject=['raw original', RuntimeInjectionFragment('goal A', RUNTIME_GOAL_SOURCE)])
    recovered = _foreground_recovery_params(original, [])
    notice = recovered.inject[-1]
    assert isinstance(notice, str) and notice == recovered.compact_continuation_injection
    assert recovered.inject[:2] == original.inject
    assert original.inject == ['raw original', RuntimeInjectionFragment('goal A', RUNTIME_GOAL_SOURCE)]
    from agent_py_agent.agent.agent_core.tool_ir_history import project_native_prompt_history

    _, first = project_native_prompt_history(recovered,
        render_prepared_prompt(prepared(recovered.inject)), conversation_state='')
    base = next(item for item in first if item.source == RUNTIME_INJECTION_SOURCE)
    assert notice in base.text
    _, output = project(first, [(RUNTIME_INJECTION_SOURCE, base.text), (RUNTIME_GOAL_SOURCE, '')])
    assert output[-1] == RuntimeFactsTurn(runtime_injection_clear_text(RUNTIME_GOAL_SOURCE), source=RUNTIME_GOAL_SOURCE)
    assert [item for item in output if item.source == RUNTIME_INJECTION_SOURCE] == [base]
