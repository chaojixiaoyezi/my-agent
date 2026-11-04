"""W3 粘性工具：真实线程CAS、native resolver与强制Compact，供应商仅本地替身。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.native_tool_protocol import resolve_native_tools
from agent_py_agent.agent.agent_core.runtime.loop_support import _initial_loop_loaded_tool_names
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.conversation.tool_discovery import persist_discovered_tools
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolProtocolSnapshot,
)
from agent_py_agent.tests.test_compactcall_websocket import _Probe, _setup


# LLM: 只有pytest根内有样本资产；脚本正文是未执行的标记，不借外部环境提供资格。
# 函数用途: 建立真实线程和一个可调用脚本。
def setup_case(tmp_path, monkeypatch, *, asset=True):
    agent, thread, prior, records = _setup(tmp_path, monkeypatch, False)
    agent.tools.catalog_default_deferral_enabled = True
    if asset:
        ScriptStore(agent.home_paths.owner_home_dir).save_script(AssetDescriptor('fixed-query', 'script', '固定查询'), 'raise AssertionError("never execute")')
    return agent, thread


# LLM: 每次重建本轮参数，工具顺序只能来自持久线程与原resolver，不能复用上一轮可变集合。
# 函数用途: 沿真实初始加载接缝生成native工具表。
def next_turn(agent, thread, *, allowed=None, scope='conversation'):
    snapshot = agent.tools.runtime_snapshot(allowed_tools=allowed)
    attrs = {'conversation_thread_id': thread.thread_id}
    original = SimpleNamespace(source='user', context_scope=scope, allowed_tools=allowed, task_attributes=attrs)
    seed = SimpleNamespace(effective_contract_snapshot=None, tool_runtime_snapshot=snapshot)
    loaded = _initial_loop_loaded_tool_names(agent, original, seed, SimpleNamespace(loaded_tool_names=set()))
    params = ToolLoopExecuteParams(user_prompt='继续', memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section='', tool_recommendations_section='', tool_context=[], effective_on_chunk=None,
        allowed_tools=allowed, write_boundary=None, task_attributes=attrs, request_id='w3', run_id='w3', task_id='',
        one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[], loaded_tool_names=loaded,
        context_scope=scope, tool_runtime_snapshot=snapshot, tool_protocol_snapshot=ToolProtocolSnapshot(
            'w3', 'native', ProviderToolCapability('test', 'local://w3', 'test', False, True, 'test')))
    return params, resolve_native_tools(agent, params)


# LLM: 比较完整数组序列化字节，不只比较工具集合；资产删除不能回收已发现的schema。
# 函数用途: 验证三轮、重建参数与资产删除后的原生前缀不变。
def test_turns_one_to_three_persist_both_names_and_never_unmount(tmp_path, monkeypatch):
    agent, thread = setup_case(tmp_path, monkeypatch)
    first_params, first = next_turn(agent, thread)
    current = agent.conversation_store.threads.require(thread.thread_id)
    assert current.loaded_tool_names == ('scripts', 'workflows')
    assert current.metadata['pinned_tool_names'] == ['scripts', 'workflows']
    assert current.metadata['discovered_tool_history'] == ['scripts', 'workflows']
    assert {'scripts', 'workflows'} <= first_params.loaded_tool_names
    second = next_turn(agent, thread)[1]
    ScriptStore(agent.home_paths.owner_home_dir).delete_script('fixed-query')
    third = next_turn(agent, thread)[1]
    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False) == json.dumps(third, ensure_ascii=False)
    assert [row['name'] for row in first][-2:] == ['scripts', 'workflows']
    assert agent.conversation_store.threads.require(thread.thread_id).metadata['pinned_tool_names'] == ['scripts', 'workflows']


# LLM: 正式tool_search只发现名称，执行权仍由快照决定；持久化和下一轮恢复必须保序。
# 函数用途: 验证新发现恰好追加一个工具而不插入旧前缀。
def test_real_tool_search_appends_exactly_one_and_survives_next_turn(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core._tool_loop_service import _load_discovered_tools

    agent, thread = setup_case(tmp_path, monkeypatch)
    params, before = next_turn(agent, thread)
    assert 'package_build' not in {row['name'] for row in before}
    outcome = agent.tools.tools['tool_search'].execute({'names': ['package_build']})
    record = {'tool': 'tool_search', 'tool_round': 1, 'tool_result_envelope': outcome.result_envelope}
    _load_discovered_tools(params, record)
    persist_discovered_tools(agent, params, record)
    after = resolve_native_tools(agent, params)
    assert after == [*before, next(row for row in after if row['name'] == 'package_build')]
    assert next_turn(agent, thread)[1] == after
    assert agent.conversation_store.threads.require(thread.thread_id).loaded_tool_names == ('scripts', 'workflows', 'package_build')


# LLM: 走正式Compact提交与线程重载，仅供应商响应是固定本地返回，不联网。
# 函数用途: 验证强制压缩后两个固定资产工具仍活跃且wire不变。
def test_forced_compaction_keeps_asset_pins_active(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.compact import (
        ConversationCompactOptions,
        prepare_conversation_context,
    )
    from agent_py_agent.agent.conversation.compact_provider_surface import (
        ConversationCompactModelSurface,
    )

    agent, thread = setup_case(tmp_path, monkeypatch)
    params, before = next_turn(agent, thread)
    _Probe(monkeypatch, False, live=False)
    current = agent.conversation_store.threads.require(thread.thread_id)
    result = prepare_conversation_context(agent, agent.conversation_store, current,
        options=ConversationCompactOptions(force=True, model_surface=ConversationCompactModelSurface(
            thread_id=thread.thread_id, loaded_tool_names=('scripts', 'workflows'))))
    assert result.compacted and result.thread.compact_generation == 1
    stored = agent.conversation_store.threads.require(thread.thread_id)
    assert stored.loaded_tool_names == ('scripts', 'workflows')
    assert stored.metadata['pinned_tool_names'] == ['scripts', 'workflows']
    assert next_turn(agent, thread)[1] == before


# LLM: 子代理身份使用正式线程本地context，隔离范围和空资产不能由拥有工具本身变成挂载资格。
# 函数用途: 验证没有资产、隔离/task_local与子代理均不持久预挂载。
@pytest.mark.parametrize('case', ['empty', 'task_local', 'isolated', 'subagent'])
def test_ineligible_contexts_do_not_mount(tmp_path, monkeypatch, case):
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )

    agent, thread = setup_case(tmp_path, monkeypatch, asset=case != 'empty')
    previous = set_current_subagent_context(agent, run_id='child') if case == 'subagent' else None
    try:
        params, wire = next_turn(agent, thread, scope=case if case in {'task_local', 'isolated'} else 'conversation')
        assert not {'scripts', 'workflows'} & params.loaded_tool_names
        assert agent.conversation_store.threads.require(thread.thread_id).loaded_tool_names == ()
    finally:
        if previous is not None:
            restore_current_subagent_context(agent, previous)


# LLM: 白名单交集和模型可见资格都不能被持久pin或历史绕过。
# 函数用途: 验证只允许scripts时不挂workflows，之后收紧权限不泄露旧工具。
def test_allowed_tools_only_reduce_mount_and_execution_surface(tmp_path, monkeypatch):
    agent, thread = setup_case(tmp_path, monkeypatch)
    params, wire = next_turn(agent, thread, allowed=['tool_search', 'scripts'])
    assert params.loaded_tool_names == {'scripts'}
    assert {row['name'] for row in wire} == {'tool_search', 'scripts'}
    assert agent.conversation_store.threads.require(thread.thread_id).metadata['pinned_tool_names'] == ['scripts']
    params, wire = next_turn(agent, thread, allowed=['tool_search'])
    assert {row['name'] for row in wire} == {'tool_search'}
    assert agent.conversation_store.threads.require(thread.thread_id).metadata['pinned_tool_names'] == ['scripts']
