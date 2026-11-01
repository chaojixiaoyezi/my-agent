"""分源快照与冻结渲染：原始文本字节、撤回和开关共用同一份输入。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.prompting_parts.builder import (
    PromptBuilder,
    PromptBuildRequest,
    PromptRenderInput,
    ToolSections,
    render_prepared_prompt,
)
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.prompting_parts.runtime_injection import (
    RUNTIME_DIRECT_CHILDREN_SOURCE,
    RUNTIME_GOAL_SOURCE,
    RUNTIME_INJECTION_SOURCE,
    RuntimeInjectionFragment,
    freeze_runtime_injections,
    runtime_injection_texts,
)
from agent_py_agent.agent.settings import AgentConfig


def prepared(values=(), *, native=True, enabled=True):
    return PromptRenderInput(
        system_prompt='stable', memory_text='memory', owner_scope='owner', dynamic='rules',
        workspace_context='workspace', injection_fragments=values, tool_catalog='tools',
        recommendations='recommendations', execution_facts='facts', user_prompt='question',
        task_and_transcript='transcript', native_tool_use=native,
        prompt_runtime_snapshots_enabled=enabled,
    )


@pytest.mark.parametrize('native', [True, False])
@pytest.mark.parametrize('enabled', [True, False])
def test_raw_join_keeps_empty_and_whitespace_bytes(native, enabled):
    values = ['', '  raw\n\n', '', '\t']
    frozen = prepared(values, native=native, enabled=enabled)
    assert frozen.injected == '\n'.join(values)
    values.append('late')
    assert frozen.injected == '\n'.join(values[:-1])
    result = render_prepared_prompt(frozen)
    if native:
        assert dict(prompt_cache_layout(result).volatile_sections)[RUNTIME_INJECTION_SOURCE] == '# Runtime Injection\n' + frozen.injected
    else:
        assert '# Runtime Injection\n' + frozen.injected + '\n\n' in result


def test_enabled_native_sections_are_full_source_snapshots_and_tombstones():
    values = ['base\n', RuntimeInjectionFragment('children A', RUNTIME_DIRECT_CHILDREN_SOURCE),
              RuntimeInjectionFragment(None, RUNTIME_GOAL_SOURCE), 'tail']
    frozen = prepared(values)
    layout = prompt_cache_layout(render_prepared_prompt(frozen))
    assert layout.volatile_sections[3:6] == (
        (RUNTIME_INJECTION_SOURCE, '# Runtime Injection\nbase\n\ntail'),
        (RUNTIME_DIRECT_CHILDREN_SOURCE, '# Runtime Injection: ' + RUNTIME_DIRECT_CHILDREN_SOURCE + '\nchildren A'),
        (RUNTIME_GOAL_SOURCE, ''),
    )
    assert 'RuntimeInjectionFragment(' not in str(render_prepared_prompt(frozen))
    assert layout.stable_prefix == prompt_cache_layout(render_prepared_prompt(replace(
        frozen, injection_fragments=(RuntimeInjectionFragment('goal B', RUNTIME_GOAL_SOURCE),),
    ))).stable_prefix


@pytest.mark.parametrize('native', [True, False])
def test_disabled_and_text_use_legacy_join_without_fragment_repr(native):
    values = ('a', RuntimeInjectionFragment('', RUNTIME_GOAL_SOURCE),
              RuntimeInjectionFragment(None, RUNTIME_DIRECT_CHILDREN_SOURCE), ' b\n')
    frozen = prepared(values, native=native, enabled=False)
    expected = '\n'.join(runtime_injection_texts(values))
    assert frozen.injected == expected
    result = render_prepared_prompt(frozen)
    assert 'RuntimeInjectionFragment(' not in result
    if native:
        sections = prompt_cache_layout(result).volatile_sections
        assert not any(source == RUNTIME_GOAL_SOURCE for source, _ in sections)
        assert (RUNTIME_INJECTION_SOURCE, '# Runtime Injection\n' + expected) in sections
    else:
        assert '# Runtime Injection\n' + expected in result


def test_builder_reads_common_switch_once_and_render_does_not_refresh(tmp_path):
    real = AgentConfig(prompt_files=[], home_context_enabled=False, assistant_progress_updates_enabled=False)
    reads = []

    class Config:
        def __getattr__(self, name):
            if name == 'prompt_runtime_snapshots_enabled':
                reads.append(name)
            return getattr(real, name)

    builder = PromptBuilder(Config(), tmp_path)
    values = [RuntimeInjectionFragment('A', RUNTIME_GOAL_SOURCE)]
    frozen = builder.prepare_render_input(PromptBuildRequest(
        'question', [], inject=values, tools=ToolSections(native_tool_use=True),
        context_scope='isolated', workspace_context_override='frozen workspace',
    ))
    assert reads == ['prompt_runtime_snapshots_enabled']
    before = render_prepared_prompt(frozen)
    values[0] = RuntimeInjectionFragment('B', RUNTIME_GOAL_SOURCE)
    real.prompt_runtime_snapshots_enabled = False
    builder.config = None
    assert render_prepared_prompt(frozen) == before
    assert frozen.injection_fragments == (RuntimeInjectionFragment('A', RUNTIME_GOAL_SOURCE),)


@pytest.mark.parametrize('bad', ['bare', b'bytes', [object()], [RuntimeInjectionFragment('a', RUNTIME_GOAL_SOURCE)] * 2])
def test_direct_frozen_input_validates_before_use(bad):
    with pytest.raises((TypeError, ValueError)):
        prepared(bad)
    with pytest.raises((TypeError, ValueError)):
        freeze_runtime_injections(bad)
