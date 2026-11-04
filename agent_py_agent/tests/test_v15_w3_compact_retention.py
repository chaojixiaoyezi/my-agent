"""W3 推荐尾段压缩保留：单模型、选择模式、原IR去重和只读恢复接缝。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.conversation import compact_provider_surface as module
from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
from agent_py_agent.tests.test_compactcall_websocket import _setup


# LLM: 仅冻结可选选择载体的分支，不运行决策模型；原快照、PromptBuilder和resolver仍是真实对象。
# 函数用途: 为两种模型模式准备可重复的压缩展示输入。
def setup_surface(tmp_path, monkeypatch, selected):
    agent, thread, prior, records = _setup(tmp_path, monkeypatch, False)

    # LLM: 使用原工具快照，只替代本测试不涉及的能力选择，不能制造新工具或执行权。
    # 函数用途: 控制选择载体是否为空。
    def presentation(host, surface, snapshot, protocol):
        return SimpleNamespace(tool_snapshot=snapshot, selection=object() if selected else None,
                               selected_skill_ids=None, required_skill_ids=())

    monkeypatch.setattr(module, '_compact_capability_presentation', presentation)
    context = RuntimeContextRequest('8.8.8.8', [], False, 'conversation')
    return agent, module.ConversationCompactModelSurface(thread_id=thread.thread_id, presentation_context=context,
                                                         context_scope='conversation')


# LLM: 通过真实推荐和builder生成当前唯一尾段，不从历史正文猜方法或借用旧section。
# 函数用途: 验证single-model和selection都保留当前Tier-2。
@pytest.mark.parametrize('selected', [False, True], ids=['single-model', 'selection'])
def test_compact_keeps_current_tier2_in_both_modes(tmp_path, monkeypatch, selected):
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore

    agent, surface = setup_surface(tmp_path, monkeypatch, selected)
    ScriptStore(agent.home_paths.owner_home_dir).save_script(AssetDescriptor('query-ip', 'script', '地址查询',
        metadata={'trigger_matchers': {'indicator_types': ['ipv4']}}), 'print("never executed")')
    prepared = module.prepare_conversation_compact_provider_surface(agent, surface, run_id='compact-w3')
    recommendations = [text for source, text in prepared.volatile_sections if source == 'prompt.tool_recommendations']
    assert len(recommendations) == 1 and recommendations[0].count('## 本轮推荐的固定方法') == 1
    assert 'query-ip' in recommendations[0]
    assert '## 本轮推荐的固定方法' not in prepared.stable_prompt_prefix
    messages = module.conversation_compact_provider_messages('', 0, [], volatile_sections=prepared.volatile_sections)
    assert str(messages).count('## 本轮推荐的固定方法') == 1


# LLM: layout禁止同源重复；连续两次准备各取当前结构化section，不把上次展示留在新对象。
# 函数用途: 验证最新section唯一性及旧对象不被热改。
@pytest.mark.parametrize('selected', [False, True], ids=['single-model', 'selection'])
def test_compact_keeps_only_latest_structured_section(tmp_path, monkeypatch, selected):
    agent, surface = setup_surface(tmp_path, monkeypatch, selected)

    versions = iter(('OLD-RECOMMENDATION', 'LATEST-RECOMMENDATION'))

    # LLM: 每次只提供一个合法同源section，展示变化来自新的宿主准备输入。
    # 函数用途: 连续提供旧、新两版结构化layout。
    def parent_prompt(host, request, params):
        return CacheStructuredPrompt('fixed-prefix', '', volatile_sections=(
            ('prompt.tool_recommendations', next(versions)),
            ('prompt.persona_updates', 'persona-update')))

    monkeypatch.setattr(module, '_compact_parent_prompt', parent_prompt)
    previous = module.prepare_conversation_compact_provider_surface(agent, surface, run_id='compact-w3-old')
    prepared = module.prepare_conversation_compact_provider_surface(agent, surface, run_id='compact-w3')
    assert prepared.volatile_sections == (('prompt.tool_recommendations', 'LATEST-RECOMMENDATION'),
                                         ('prompt.persona_updates', 'persona-update'))
    assert 'OLD-RECOMMENDATION' not in str(module.conversation_compact_provider_messages('', 0, [],
        volatile_sections=prepared.volatile_sections))
    assert previous.volatile_sections[0] == ('prompt.tool_recommendations', 'OLD-RECOMMENDATION')


# LLM: 原IR合同是同文本不重复、空文本不撤销旧事实；此测试明确记录既有语义，不修改只读模块。
# 函数用途: 验证同输入去重、变化追加、空段忽略和原输入不变。
def test_runtime_facts_repeat_change_and_empty_semantics_are_preserved():
    from agent_py_agent.agent.agent_core.tool_ir_history import project_native_prompt_history

    params = SimpleNamespace(tool_ir_history=[UserTurn('原始任务')], task_attributes={}, compact_context=None)
    first_prompt = CacheStructuredPrompt('fixed', '', volatile_sections=(('prompt.tool_recommendations', 'SAME'),))
    provider_prompt, first = project_native_prompt_history(params, first_prompt)
    assert len(params.tool_ir_history) == 1
    params.tool_ir_history = first
    provider_prompt, repeated = project_native_prompt_history(params, first_prompt)
    assert repeated == first
    changed_prompt = CacheStructuredPrompt('fixed', '', volatile_sections=(('prompt.tool_recommendations', 'LATEST'),))
    provider_prompt, changed = project_native_prompt_history(params, changed_prompt)
    assert changed[-1] == RuntimeFactsTurn('LATEST', source='prompt.tool_recommendations')
    params.tool_ir_history = changed
    provider_prompt, empty = project_native_prompt_history(params, CacheStructuredPrompt('fixed', '',
        volatile_sections=(('prompt.tool_recommendations', ''),)))
    assert empty == changed
    assert [turn.text for turn in empty if isinstance(turn, RuntimeFactsTurn)] == ['SAME', 'LATEST']


# LLM: 只读恢复模块从冻结prompt_input取唯一当前推荐，不重新读库存或调用选择模型。
# 函数用途: 验证恢复摘要接缝保持一份最新推荐，旧IR仍保持原件。
def test_readonly_recovery_surface_keeps_one_current_section():
    from agent_py_agent.agent.agent_core.compact_request_recovery import _summary_surface
    from agent_py_agent.agent.prompting_parts.builder import PromptRenderInput
    from agent_py_agent.tests.test_compact_active_projection import _fixture

    context, params, frozen, retained = _fixture()
    prompt = PromptRenderInput(system_prompt='fixed', memory_text='', owner_scope='', dynamic='', workspace_context='',
        injection_fragments=(), tool_catalog='', recommendations='LATEST-RECOMMENDATION', execution_facts='',
        user_prompt='继续', task_and_transcript='继续', native_tool_use=True)
    frozen = replace(frozen, prompt_input=prompt, native_tools=(), system_instruction='system',
                     tool_ir_history=(*frozen.tool_ir_history, RuntimeFactsTurn('OLD-RECOMMENDATION', source='prompt.tool_recommendations')))
    surface = _summary_surface(frozen)
    assert surface.volatile_sections == (('prompt.tool_recommendations', 'LATEST-RECOMMENDATION'),)
    assert frozen.tool_ir_history[-1].text == 'OLD-RECOMMENDATION'
