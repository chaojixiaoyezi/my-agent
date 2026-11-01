"""真实 IR 是唯一去重基线；清空、ABA 与来源撤回不依赖进程 seen-set。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_ir_history as mod
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
from agent_py_agent.agent.prompting_parts.runtime_injection import (
    RUNTIME_DIRECT_CHILDREN_SOURCE,
    RUNTIME_GOAL_SOURCE,
    RUNTIME_INJECTION_SOURCE,
    runtime_injection_clear_text,
)


def project(history, sections):
    original = list(history)
    params = SimpleNamespace(tool_ir_history=history)
    provider, projected = mod.project_native_prompt_history(
        params, CacheStructuredPrompt('stable', volatile_sections=tuple(sections), canonical_user_turn='question'),
        conversation_state='',
    )
    assert params.tool_ir_history is history and len(history) == len(original)
    assert all(a is b for a, b in zip(history, original))
    return provider, projected


def test_public_record_clear_aba_and_default_empty_semantics():
    params = SimpleNamespace(tool_ir_history=[])
    source = RUNTIME_GOAL_SOURCE
    assert not mod.record_runtime_facts_turn_ir(params, '', source=source, clear_empty=True)
    assert mod.record_runtime_facts_turn_ir(params, 'A', source=source, clear_empty=True)
    assert not mod.record_runtime_facts_turn_ir(params, 'A', source=source, clear_empty=True)
    assert mod.record_runtime_facts_turn_ir(params, 'B', source=source, clear_empty=True)
    assert mod.record_runtime_facts_turn_ir(params, 'A', source=source, clear_empty=True)
    assert not mod.record_runtime_facts_turn_ir(params, '', source=source)
    assert not mod.record_runtime_facts_turn_ir(params, '  ', source=source, clear_empty=True)
    assert mod.record_runtime_facts_turn_ir(params, '', source=source, clear_empty=True)
    assert not mod.record_runtime_facts_turn_ir(params, '', source=source, clear_empty=True)
    assert mod.record_runtime_facts_turn_ir(params, 'A', source=source, clear_empty=True)
    assert [row.text for row in params.tool_ir_history] == ['A', 'B', 'A', runtime_injection_clear_text(source), 'A']


@pytest.mark.parametrize('source', ['', 'runtime.guidance', 'prompt.runtime_injectionX', None])
def test_non_runtime_clear_fails_before_creating_history(source):
    params = SimpleNamespace()
    with pytest.raises(ValueError):
        mod.record_runtime_facts_turn_ir(params, '', source=source, clear_empty=True)
    assert not hasattr(params, 'tool_ir_history')


def test_projection_appends_only_changed_source_and_preserves_full_bytes():
    base = RuntimeFactsTurn('base A', source=RUNTIME_INJECTION_SOURCE)
    child = RuntimeFactsTurn('children A', source=RUNTIME_DIRECT_CHILDREN_SOURCE)
    user = UserTurn('[runtime-injection-cleared source=fake]')
    history = [base, child, user]
    provider, output = project(history, [(RUNTIME_INJECTION_SOURCE, 'base A'),
        (RUNTIME_DIRECT_CHILDREN_SOURCE, 'children B\n\nfull'), (RUNTIME_GOAL_SOURCE, '')])
    assert output[:3] == history
    assert len(output) == 4
    assert output[-1] == RuntimeFactsTurn('children B\n\nfull', source=RUNTIME_DIRECT_CHILDREN_SOURCE)
    assert prompt_sections(provider) == ()
    _, cleared = project(output, [(RUNTIME_INJECTION_SOURCE, 'base A'), (RUNTIME_DIRECT_CHILDREN_SOURCE, '')])
    assert cleared[-1] == RuntimeFactsTurn(runtime_injection_clear_text(RUNTIME_DIRECT_CHILDREN_SOURCE), source=RUNTIME_DIRECT_CHILDREN_SOURCE)
    _, again = project(cleared, [(RUNTIME_INJECTION_SOURCE, 'base A'), (RUNTIME_DIRECT_CHILDREN_SOURCE, '')])
    assert len(again) == len(cleared)
    _, restored = project(cleared, [(RUNTIME_DIRECT_CHILDREN_SOURCE, 'children A')])
    assert restored[-1].text == 'children A'
    # 回滚/重载完全由仍存活的 IR 决定，没有跨请求影子状态。
    _, reloaded = project([], [(RUNTIME_DIRECT_CHILDREN_SOURCE, 'children A')])
    assert reloaded == [RuntimeFactsTurn('children A', source=RUNTIME_DIRECT_CHILDREN_SOURCE)]


def prompt_sections(prompt):
    from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
    return prompt_cache_layout(prompt).volatile_sections


def test_final_conversation_fact_uses_updated_layout_map():
    params = SimpleNamespace(tool_ir_history=[])
    _, output = mod.project_native_prompt_history(params,
        CacheStructuredPrompt('stable', volatile_sections=(('conversation.runtime', 'first'),)),
        conversation_state='second')
    assert [row.text for row in output] == ['first', 'second']
    assert params.tool_ir_history == []


def test_projection_latest_index_has_one_reverse_pass():
    reads = [0]

    class CountedFact(RuntimeFactsTurn):
        def __getattribute__(self, name):
            if name == 'source':
                reads[0] += 1
            return super().__getattribute__(name)

    sources = [RUNTIME_INJECTION_SOURCE, RUNTIME_DIRECT_CHILDREN_SOURCE, RUNTIME_GOAL_SOURCE,
               'prompt.runtime_injection.delivery_contract', 'prompt.runtime_injection.public_receipt', 'conversation.runtime']
    history = [CountedFact('old', source=source) for source in sources]
    history += [CountedFact('noise', source=f'unrelated.{index}') for index in range(500)]
    _, projected = mod.project_native_prompt_history(SimpleNamespace(tool_ir_history=history),
        CacheStructuredPrompt('stable', volatile_sections=tuple((source, 'new') for source in sources[:-1])),
        conversation_state='current')
    assert reads[0] <= len(history) + 40
    assert len(projected) == len(history) + len(sources)


def test_actual_tool_pair_retirement_retains_latest_complete_and_withdrawn_sources():
    from copy import deepcopy

    from agent_py_agent.agent.backends.tool_ir import CompactionSummary
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_history_call,
        canonical_history_result,
    )

    params = SimpleNamespace(tool_ir_history=[])
    user = UserTurn('real late input')
    for index in range(3):
        mod.record_runtime_facts_turn_ir(params, f'children-{index}\nfull snapshot', source=RUNTIME_DIRECT_CHILDREN_SOURCE)
        mod.record_runtime_facts_turn_ir(params, 'goal A' if index == 0 else '', source=RUNTIME_GOAL_SOURCE, clear_empty=True)
        call = canonical_history_call('read_file', {'path': f'{index}.txt'}, call_id=f'call-{index}',
            run_id='run', attempt_id='attempt', turn_id=f'turn-{index}')
        mod.record_tool_call_ir(params, tool_rounds=index + 1, call=call, result=canonical_history_result(call, 'full result'))
    params.tool_ir_history.append(user)
    assert mod.replace_compaction_summary_ir(params, 'complete replacement summary')
    assert mod.drop_tool_call_pairs(params, {'call-0', 'call-1', 'call-2'}, drop_completed_tool_turns=True) == 3
    facts = [item for item in params.tool_ir_history if isinstance(item, RuntimeFactsTurn)]
    assert facts == [RuntimeFactsTurn(runtime_injection_clear_text(RUNTIME_GOAL_SOURCE), source=RUNTIME_GOAL_SOURCE),
                     RuntimeFactsTurn('children-2\nfull snapshot', source=RUNTIME_DIRECT_CHILDREN_SOURCE)]
    assert any(isinstance(item, CompactionSummary) for item in params.tool_ir_history)
    assert params.tool_ir_history[-1] is user
    restored = deepcopy(params.tool_ir_history)
    _, projected = project(restored, [(RUNTIME_DIRECT_CHILDREN_SOURCE, 'children-2\nfull snapshot'), (RUNTIME_GOAL_SOURCE, '')])
    assert projected == restored
    _, changed = project(restored, [(RUNTIME_GOAL_SOURCE, 'goal A')])
    assert changed[-1] == RuntimeFactsTurn('goal A', source=RUNTIME_GOAL_SOURCE)
