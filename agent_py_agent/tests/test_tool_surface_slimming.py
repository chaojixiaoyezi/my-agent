# LLM: 回归真实派工授权投影、规范宿主caller和会话回执下一跳；模型/后台启动不作验收，无外部网络。
# 模块用途: 防止目录动作跨代扩权、Audit旧输入及历史工具提示重新引入旧协议。
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
from agent_py_agent.agent.capability.session_search_tool import SessionSearchTool
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.capability_scope import bind_creation_tool_authority
from agent_py_agent.agent.subagents.role_templates import active_model_subagent_tools
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall


# LLM: 只建临时owner和echo依赖；不启动child模型，不读取真实home。
# 函数用途: 提供真实注册表和创建参数装配器。
@pytest.fixture
def agent(tmp_path):
    return SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home')), tmp_path)


# LLM: 使用真实执行器与该冻结快照schema，不直接mock发现handler或绕过执行权限。
# 函数用途: 证明目录可用但搜索/加载在执行入口被拒绝。
def assert_catalog_only(snapshot, tmp_path):
    runtime = snapshot.runtime('tool_search')
    assert runtime is not None
    results = []
    for action in ('catalog', 'search'):
        args = {'action': action, **({'names': ['read_file']} if action == 'search' else {})}
        call = ToolCall(call_id='scope-' + action, tool_name='tool_search', arguments=args,
            source_protocol='native', schema_hash=runtime.model_spec.schema_hash,
            run_id=snapshot.run_id or 'r', turn_id='t', attempt_id='a')
        results.append(ToolExecutor().execute(ToolExecutorRequest(call=call, runtime_snapshot=snapshot,
            workspace_root=tmp_path, operation_store_required=False)).result)
    assert results[0].ok, results[0].output
    assert not results[1].ok, results[1].output
    assert runtime.model_spec.input_schema['properties']['action']['enum'] == ['catalog']


# LLM: 真正经过父快照绑定和create_run_params默认角色求交，两轮形成子/孙；不以手工拼子权限冒充派生。
# 函数用途: 默认角色加回tool_search也不能扩大catalog-only父级动作范围。
def test_catalog_only_survives_child_and_grandchild_creation(agent, tmp_path):
    snapshot = agent.tools.runtime_snapshot(allowed_tools=['list_tools', 'create_subagents', 'read_file'], run_id='root')
    assert_catalog_only(snapshot, tmp_path)
    for generation in ('child', 'grandchild'):
        with bind_creation_tool_authority(snapshot):
            spec = create_run_params(agent, {'goal': generation, 'role': 'coordinator'}, generation,
                ['tool_search', 'create_subagents', 'read_file'])
        snapshot = agent.tools.runtime_snapshot(allowed_tools=active_model_subagent_tools(spec.allowed_tools), run_id=generation)
        assert_catalog_only(snapshot, tmp_path)


# LLM: 将真实快照或创建参数的allowed_tools写JSON再由全新registry重建，验证丢掉进程态后仍不能恢复search。
# 函数用途: 覆盖持久化字段恢复与当前owner禁用优先，不声称完整后台resume已运行。
@pytest.mark.parametrize('origin', ['snapshot', 'created_child'])
def test_catalog_only_persisted_allowlist_rebuild_and_disable(agent, tmp_path, origin):
    snapshot = agent.tools.runtime_snapshot(allowed_tools=['list_tools', 'read_file'], run_id='root')
    if origin == 'snapshot':
        allowed = sorted(snapshot.allowed_tools)
    else:
        with bind_creation_tool_authority(snapshot):
            allowed = create_run_params(agent, {'goal': '恢复'}, '恢复', ['tool_search', 'read_file']).allowed_tools
    path = tmp_path / 'persisted-child.json'
    path.write_text(json.dumps({'allowed_tools': allowed}))
    recovered = active_model_subagent_tools(json.loads(path.read_text())['allowed_tools'])
    fresh = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home')), tmp_path)
    assert_catalog_only(fresh.tools.runtime_snapshot(allowed_tools=recovered, run_id='restored'), tmp_path)
    for disabled in ('tool_search', 'list_tools'):
        fresh.tools.disabled_tool_names.add(disabled)
        assert fresh.tools.runtime_snapshot(allowed_tools=recovered).runtime('tool_search') is None
        fresh.tools.disabled_tool_names.remove(disabled)


# LLM: 公共规范search授权不应被本次最窄迁移误变成catalog；显式旧grant在全权父下仍仅catalog。
# 函数用途: 避免修复把所有子代理发现能力直接关掉，或把显式旧grant丢失。
@pytest.mark.parametrize('grant', ['tool_search', 'list_tools'])
def test_full_parent_preserves_requested_discovery_scope(agent, tmp_path, grant):
    with bind_creation_tool_authority(agent.tools.runtime_snapshot(run_id='root')):
        spec = create_run_params(agent, {'goal': '核对', '_exact_allowed_tools': True}, '核对', [grant])
    snapshot = agent.tools.runtime_snapshot(allowed_tools=spec.allowed_tools)
    if grant == 'list_tools':
        assert_catalog_only(snapshot, tmp_path)
    else:
        assert snapshot.runtime('tool_search').model_spec.input_schema['properties']['action']['enum'] == ['search', 'catalog']


# LLM: 只替代解析后的准备/创建阶段，真实服务和create_items_from_params不替换，也不创建真实后台工作者。
# 函数用途: 观察宿主caller穿过真实规范解析器的完整item，不在mock服务内重复解析来制造通过。
@pytest.fixture
def audit_parser_capture(monkeypatch):
    from agent_py_agent.agent.agent_core import orchestration_tools

    captured = []

    # LLM: 进入此缝隙说明正式解析已成功；仅模拟创建结果，不模拟参数校验。
    # 函数用途: 固定返回已记录但未启动的回执并保留解析后字段供断言。
    def prepared(agent, items, params):
        captured.extend(items)
        return ToolHandlerOutcome('create_subagents', True, json.dumps({'created_run_ids': ['child-r'], 'auto_start': {'status': 'not_attempted'}}))

    monkeypatch.setattr(orchestration_tools, '_prepare_items', prepared)
    return captured


# LLM: 调用真实发布来源caller再进入真实服务，旧顶层形状会在准备mock之前直接被拒。
# 函数用途: 验证首次Audit来源派工的goal、source、资料与值守标记完整迁移。
def test_published_audit_source_uses_canonical_items(audit_parser_capture):
    from agent_py_agent.agent.ingestion.source_worker import _provision_published_audit_source

    agent = SimpleNamespace(config=SimpleNamespace(enable_subagents=True))
    result = _provision_published_audit_source(agent, {'source_id': 'source-a', 'source_profile_ref': 'profile.md', 'document_refs': ['doc.md']})
    assert result['ok'], result
    item = audit_parser_capture[0].params
    assert item['audit_source_id'] == 'source-a' and item['role'] == 'worker' and item['long_running'] is True
    assert item['context_manifest']['required_read_paths'] == ['profile.md', 'doc.md']
    assert 'source-a' in item['goal']


# LLM: 第二宿主构造入口使用真实WatchState/属性构造；引擎不用运行，创建在解析后截断。
# 函数用途: 保留Audit父根谱系、精确scope及属性，不把它们移进公共模型schema。
def test_audit_worker_constructor_keeps_host_lineage_and_exact_scope(tmp_path, audit_parser_capture):
    from agent_py_agent.agent.agent_core.orchestration.tool_grants import subagent_allowed_tools
    from agent_py_agent.agent.agent_core.orchestration_tools import execute_create_subagents_service
    from agent_py_agent.agent.common.audit_activation import (
        audit_source_worker_key,
        audit_worker_tool_scope,
    )
    from agent_py_agent.agent.ingestion.config import IngestTuning
    from agent_py_agent.agent.ingestion.source_worker import _source_worker_create_params
    from agent_py_agent.agent.ingestion.watch_state import WatchState

    agent = SimpleNamespace(config=SimpleNamespace(enable_subagents=True))
    state = WatchState(watch_id='watch-a', owner_home=tmp_path, source_url='file:///input.jsonl',
        source_id='source-a', tuning=IngestTuning(), engine=None, audit_root_task_id='audit-root')
    params = _source_worker_create_params(agent, state, audit_source_worker_key('audit-root', 'watch-a'))
    result = execute_create_subagents_service(agent, params)
    assert result.ok, (result.error_code, result.output)
    item = audit_parser_capture[0].params
    assert item['parent_id'] == item['root_id'] == 'audit-root'
    assert item['_exact_allowed_tools'] is True and item['_include_goal_file_hints'] is False
    assert item['attributes'] and item['context_manifest'] and item['long_running'] is True
    assert subagent_allowed_tools(item) == list(audit_worker_tool_scope(item['attributes']))


# LLM: 仅解析回执内明确JSON参数，不补mode、不归一旧语法，保证提示本身可直接重放。
# 函数用途: 提取历史工具给模型的下一跳调用示例。
def hint_calls(hint):
    import re

    return [json.loads(match.group()) for match in re.finditer(r'\{[^{}]+\}', hint)]


# LLM: 能力申请与派工共用父快照事实；只读目录父级不得通过grant旁路给无目录权限的后代授完整search。
# 函数用途: 防止恢复和再授权链把动作限制退化为只比较工具名。
def test_catalog_parent_cannot_grant_full_search_through_capability_request(agent):
    from agent_py_agent.agent.subagents.capability_scope import (
        DIRECT_PARENT_TOOL_AUTHORITY_ATTR,
        creation_tool_authority_snapshot,
        direct_parent_tool_authority,
    )

    parent = agent.tools.runtime_snapshot(allowed_tools=['list_tools'], run_id='parent')
    task = SimpleNamespace(parent_id='parent', attributes={DIRECT_PARENT_TOOL_AUTHORITY_ATTR: creation_tool_authority_snapshot(parent)})
    decision = direct_parent_tool_authority(SimpleNamespace(tools=agent.tools), task, ['tool_search'])
    assert decision.grantable_tools == ()
    assert decision.unavailable_tools == ('tool_search',)


# LLM: 遗留child没有父快照/父记录时不能借owner默认全权授权；用真实执行器核对拒绝后的恢复范围。
# 函数用途: 守住缺失父证据的旧白名单恢复，不让兼容分支复活search。
def test_legacy_child_missing_parent_cannot_expand_on_grant(agent, tmp_path):
    from agent_py_agent.agent.subagents.capability_scope import direct_parent_tool_authority

    task = SimpleNamespace(parent_id='missing-parent', allowed_tools=['list_tools'], attributes={})
    decision = direct_parent_tool_authority(SimpleNamespace(tools=agent.tools), task, ['tool_search'])
    assert decision.grantable_tools == ()
    assert decision.snapshot_error_code == 'PARENT_CREATION_SNAPSHOT_MISSING'
    restored = active_model_subagent_tools([*task.allowed_tools, *decision.grantable_tools])
    assert_catalog_only(agent.tools.runtime_snapshot(allowed_tools=restored, run_id='legacy-restored'), tmp_path)


# LLM: 内部item透传不会改变公共模型入参；限制令牌只保存在硬授权中，不成为可调用注册工具。
# 函数用途: 证明修复没有把宿主血缘字段或影子工具暴露给模型。
def test_host_fields_and_catalog_token_not_public_schema(agent):
    snapshot = agent.tools.runtime_snapshot()
    schema = snapshot.runtime('create_subagents').model_spec.input_schema
    forbidden = {'parent_id', 'root_id', 'attributes', '_exact_allowed_tools', 'context_manifest', '_include_goal_file_hints'}
    assert not forbidden & set(schema['properties'])
    assert not forbidden & set(schema['properties']['items']['items']['properties'])
    assert 'tool_search:catalog' not in snapshot.available_tool_names


# LLM: 真正临时LocalStore空结果，提示至少给出一个可直接执行的规范下一跳；不靠schema存在断言代替行为。
# 函数用途: 空检索/空浏览/不存在锚点的恢复提示不再引导无mode调用。
@pytest.mark.parametrize('params', [
    {'mode': 'search', 'query': '空库唯一关键词'}, {'mode': 'recent'}, {'mode': 'around', 'around_id': 'missing'}])
def test_empty_history_hint_executes_explicit_next_hop(tmp_path, params):
    tool = SessionSearchTool(SimpleNamespace(local_store=LocalStore(tmp_path / 'local.db')))
    result = tool.execute(params)
    assert result.ok, result.output
    calls = hint_calls(json.loads(result.output)['hint'])
    assert calls, result.output
    for call in calls:
        assert 'mode' in call
        follow = tool.execute(call)
        assert follow.ok, (call, follow.error_code, follow.output)


# LLM: 用真实canonical会话及派生索引，依提示读取/翻看/分页；不自行添mode，保留原输出mode和scope。
# 函数用途: 成功检索与本会话空命中的下一跳示例真的可执行。
@pytest.mark.parametrize('mode,query', [('search', '金额'), ('thread_search', '金额'),
    ('thread_search', '绝不会命中的唯一关键词XYZ'), ('recent', None), ('around', None)])
def test_history_success_hints_execute_against_real_store(tmp_path, mode, query):
    from agent_py_agent.tests.test_session_history_read import _owner

    owner = _owner(tmp_path)
    tool = SessionSearchTool(owner.agent)
    params = {'mode': mode}
    if query:
        params['query'] = query
    if mode == 'around':
        recents = owner.agent.local_store.list_recent(limit=1)
        params['around_id'] = recents[0].id
    result = tool.execute(params)
    assert result.ok, result.output
    page = json.loads(result.output)
    calls = hint_calls(page['hint'])
    assert calls, page['hint']
    for call in calls:
        assert 'mode' in call
        follow = tool.execute(call)
        assert follow.ok, (call, follow.error_code, follow.output)
        if call['mode'] == 'message':
            assert json.loads(follow.output)['found'] is True
