# LLM: 回归真实创建/递归/重载的目录动作授权、规范宿主caller及提示下一跳；后台启动替身不作真实模型验收，无外部网络。
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


# LLM: 仅测试父快照绑定和参数装配，不涵盖正式创建、持久化或递归调度；完整链路另有生产入口回归。
# 函数用途: 核对参数装配层的角色默认值不扩大catalog-only父级动作范围。
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


# LLM: 只替代后台启动；实际走scoped工具、角色归一、manager保存、runner递归调度及全新Agent重载，无外部模型。
# 函数用途: 同时锁住第一代落盘和显式/继承两种孙代理的目录限制及真实父记录能力申请。
@pytest.mark.parametrize('explicit', [True, False])
@pytest.mark.parametrize('legacy_record', [False, True])
@pytest.mark.parametrize('full_parent', [False, True])
def test_production_catalog_scope_spawn_reload_and_capability(agent, tmp_path, monkeypatch, explicit, legacy_record, full_parent):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.orchestration import lifecycle
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    from agent_py_agent.agent.subagents.capability_scope import direct_parent_tool_authority
    from agent_py_agent.agent.tooling.models import ToolInvocationContext

    monkeypatch.setattr(lifecycle, 'auto_start_tasks',
        lambda _agent, _tasks, _params, **_kw: {'status': 'not_needed', 'run_ids': []})
    root = agent.tools.runtime_snapshot(allowed_tools=['tool_search' if full_parent else 'list_tools', 'create_subagents', 'read_file'], run_id='root')
    first = {'goal': '协调只读核对', 'role': 'coordinator'}
    if full_parent:
        first.update(allowed_tools=['list_tools', 'create_subagents', 'read_file'], tool_preset='read_only')
    created = CreateSubagentsTool(agent).execute_scoped(
        {'items': [first]}, ToolInvocationContext(root))
    assert created.ok, created.output
    child_id, = json.loads(created.output)['created_run_ids']
    child = agent.subagents.load(child_id)
    if legacy_record:
        # 仅此变体显式模拟旧版落盘字段，不宣称执行过跨版本升级；普通变体绝不手工拼造子代理权限。
        agent.subagents.save(replace(child, allowed_tools=['list_tools', 'create_subagents', 'read_file']))
    fresh = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home')), tmp_path)
    child = fresh.subagents.load(child_id)
    child_context = fresh.subagents.runner_context.build_execution_context(child_id)
    child_snapshot = fresh.tools.runtime_snapshot(allowed_tools=child_context.allowed_tools, run_id=child_id)
    item = {'goal': '核对目录，显式授权' if explicit else '核对目录，继承授权'}
    if explicit:
        item['allowed_tools'] = ['tool_search', 'read_file']
    previous = set_current_subagent_context(fresh, run_id=child_id, task_attributes=child.attributes)
    try:
        created = CreateSubagentsTool(fresh).execute_scoped({'items': [item]}, ToolInvocationContext(child_snapshot))
    finally:
        restore_current_subagent_context(fresh, previous)
    assert created.ok, created.output
    grand_id, = json.loads(created.output)['created_run_ids']
    restored = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home')), tmp_path)
    for run_id in (child_id, grand_id):
        stored = restored.subagents.load(run_id)
        if run_id == grand_id or not legacy_record:
            assert 'list_tools' not in stored.allowed_tools
            assert {'tool_search', 'tool_search:catalog'} <= set(stored.allowed_tools)
        context = restored.subagents.runner_context.build_execution_context(run_id)
        snapshot = restored.tools.runtime_snapshot(allowed_tools=context.allowed_tools, run_id=run_id)
        assert_catalog_only(snapshot, tmp_path)
    grand = restored.subagents.load(grand_id)
    assert grand.parent_id == child_id
    decision = direct_parent_tool_authority(restored, grand, ['tool_search'])
    assert decision.authority_source == 'parent_run_execution_context'
    assert decision.grantable_tools == ()
    assert decision.unavailable_tools == ('tool_search',)


# LLM: 走真实快照与首轮原生定义投影；目录不能加载时，已授权默认收起工具不能失去可见定义。
# 函数用途: 验证目录只读者仍能直接看见本夹具里实际就绪的默认收起工具。
@pytest.mark.parametrize('preloaded', [False, True])
def test_catalog_only_keeps_authorized_deferred_tools_visible(agent, preloaded):
    from dataclasses import replace

    from agent_py_agent.agent.tooling.self_context import _snapshot_model_names

    allowed = ['list_tools', 'read_file', 'schedule', 'send_message', 'memory_search']
    snapshot = agent.tools.runtime_snapshot(allowed_tools=allowed)
    if preloaded:
        snapshot = replace(snapshot, preloaded_tool_names=('read_file',))
    expected = {runtime.model_spec.name for runtime in snapshot.runtimes if runtime.model_spec.hints.default_deferred}
    # 本夹具未绑定消息/记忆服务，不能把它们的不可用误报成默认折叠；schedule必须实际就绪且被检查。
    assert 'schedule' in expected
    visible = {spec.name for spec in agent.tools.specs_for_snapshot(snapshot)}
    assert {'tool_search', 'read_file', *expected} <= visible
    assert {'tool_search', 'read_file', *expected} <= _snapshot_model_names(snapshot)


# LLM: 使用真实catalog handler并检查完整归档正文而非live摘要；令牌留在宿主快照，不进入用户目录。
# 函数用途: 防止内部动作限制名被误当成可调用工具暴露。
def test_catalog_archive_omits_internal_action_grants(agent):
    from agent_py_agent.agent.tooling.models import ToolInvocationContext

    snapshot = agent.tools.runtime_snapshot(allowed_tools=['list_tools', 'read_file'])
    result = agent.tools.tools['tool_search'].execute_scoped({'action': 'catalog'}, ToolInvocationContext(snapshot))
    assert result.ok, result.output
    payload = json.loads(result.output)
    assert 'tool_search' in payload['allowed_tools']
    assert 'tool_search:catalog' not in result.output
    assert 'tool_search:catalog' in snapshot.allowed_tools


# LLM: 模型派Audit叶时宿主禁用提示在后续准备层才出现，规范解析必须提前按结构化source_id挡住goal文件提示。
# 函数用途: 显式False和Audit来源均不从自然语言扩大required_read_paths，普通派工仍保留原提示行为。
@pytest.mark.parametrize('scope', [{'_include_goal_file_hints': False}, {'audit_source_id': 'source-a'}, {}])
def test_item_goal_path_hints_respect_structured_scope(tmp_path, scope):
    from agent_py_agent.agent.agent_core.orchestration.create_payload import (
        create_items_from_params,
    )

    own = tmp_path / 'own.md'
    sibling = tmp_path / 'sibling.md'
    own.write_text('own')
    sibling.write_text('sibling')
    items = create_items_from_params({'items': [{'goal': f'参考 {sibling}',
        'context_manifest': {'required_read_paths': [str(own)]}, **scope}]})
    assert not isinstance(items, str), items
    refs = items[0].params['required_read_paths']
    assert str(own) in refs
    assert (str(sibling) in refs) is (not scope)


# LLM: 必经真实预设合并、创建策略和正式角色归一；只读旧grant不能被read_only/coding默认发现工具冲宽。
# 函数用途: 保留显式目录动作来源，兼顾完整search正例与内部窄令牌。
@pytest.mark.parametrize('preset', ['read_only', 'coding'])
@pytest.mark.parametrize('grants', [['list_tools'], ['tool_search:catalog'], ['tool_search'], ['list_tools', 'tool_search']])
def test_explicit_discovery_scope_survives_preset_and_normalization(agent, tmp_path, preset, grants):
    from agent_py_agent.agent.agent_core.orchestration.tool_grants import subagent_allowed_tools
    from agent_py_agent.agent.subagents.services.base import SubAgentBaseService

    raw = {'goal': '核对显式发现权限', 'allowed_tools': grants, 'tool_preset': preset}
    merged = subagent_allowed_tools(raw)
    with bind_creation_tool_authority(agent.tools.runtime_snapshot(run_id='root')):
        params = create_run_params(agent, raw, raw['goal'], merged)
    normalized = SubAgentBaseService(agent.subagents)._normalized_create_params(params)
    snapshot = agent.tools.runtime_snapshot(allowed_tools=normalized.allowed_tools)
    if 'tool_search' not in grants:
        assert_catalog_only(snapshot, tmp_path)
    else:
        assert snapshot.runtime('tool_search').model_spec.input_schema['properties']['action']['enum'] == ['search', 'catalog']


# LLM: 旧v1只有工具名不是search动作证据；真实裁决后用执行器验证合并授权不能放大原目录权限。
# 函数用途: 缺失/空动作证据必须拒绝，明确完整grant仍可授予；本例父记录确实不存在。
@pytest.mark.parametrize('evidence', [None, [], ['tool_search'], ['tool_search', 'tool_search:catalog']])
@pytest.mark.parametrize('child_grants', [['list_tools'], ['tool_search', 'tool_search:catalog']])
def test_legacy_parent_snapshot_requires_action_evidence(agent, tmp_path, evidence, child_grants):
    from agent_py_agent.agent.subagents.capability_scope import (
        DIRECT_PARENT_TOOL_AUTHORITY_ATTR,
        direct_parent_tool_authority,
    )

    snapshot = {'schema_version': 'direct_parent_tool_authority.v1', 'parent_run_id': 'missing-parent',
        'available_tool_names': ['read_file', 'tool_search']}
    if evidence is not None:
        snapshot['tool_grants'] = evidence
    task = SimpleNamespace(parent_id='missing-parent', allowed_tools=child_grants,
        attributes={DIRECT_PARENT_TOOL_AUTHORITY_ATTR: snapshot})
    decision = direct_parent_tool_authority(agent, task, ['tool_search'])
    assert decision.authority_source == 'parent_creation_runtime_snapshot'
    if evidence == ['tool_search']:
        assert decision.grantable_tools == ('tool_search',)
    else:
        assert decision.grantable_tools == ()
        if not evidence:
            assert decision.snapshot_error_code == 'PARENT_TOOL_ACTION_EVIDENCE_MISSING'
        allowed = [*child_grants, *decision.grantable_tools]
        assert_catalog_only(agent.tools.runtime_snapshot(allowed_tools=allowed), tmp_path)


# LLM: 递归策略的补充正反例；全权父下显式旧目录仍窄，显式完整search仍宽，不以全部关闭发现来修复扩权。
# 函数用途: 核对两种角色在完整父授权下保留子级显式发现动作意图。
@pytest.mark.parametrize('role', ['worker', 'coordinator'])
@pytest.mark.parametrize('grant', ['list_tools', 'tool_search', 'tool_search:catalog'])
def test_hierarchy_keeps_explicit_discovery_intent_under_full_parent(agent, tmp_path, role, grant):
    from agent_py_agent.agent.subagents.services.hierarchy.tool_policy import (
        ToolPolicyRequest,
        scheduled_child_tools,
    )

    tools = scheduled_child_tools(ToolPolicyRequest(parent_tools=['tool_search', 'read_file', 'create_subagents'],
        spec=SimpleNamespace(allowed_tools=[grant, 'read_file'], role=role), extra_write_roots=[], role=role))
    snapshot = agent.tools.runtime_snapshot(allowed_tools=tools)
    if grant != 'tool_search':
        assert_catalog_only(snapshot, tmp_path)
    else:
        runtime = snapshot.runtime('tool_search')
        call = ToolCall(call_id='full-search', tool_name='tool_search', arguments={'action': 'search', 'names': ['read_file']},
            source_protocol='native', schema_hash=runtime.model_spec.schema_hash, run_id='full', turn_id='t', attempt_id='a')
        result = ToolExecutor().execute(ToolExecutorRequest(call=call, runtime_snapshot=snapshot,
            workspace_root=tmp_path, operation_store_required=False)).result
        assert result.ok, result.output
        assert json.loads(result.output)['loaded_for_next_model_call'] == ['read_file']


# LLM: 旧v1快照经真实manager角色归一落盘，再从全新Agent派孙并重载；只替代后台启动，canonical与真实执行都核动作证据。
# 函数用途: 复现并锁住缺失/空证据的正式创建扩权，递归显式及继承两路均保目录，完整证据可实际搜索。
@pytest.mark.parametrize('evidence', [None, [], ['tool_search'], ['tool_search', 'tool_search:catalog']])
@pytest.mark.parametrize('explicit', [False, True])
def test_legacy_creation_snapshot_is_narrowed_before_canonical_save(agent, tmp_path, monkeypatch, evidence, explicit):
    from agent_py_agent.agent.agent_core.orchestration import lifecycle
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    from agent_py_agent.agent.subagents.capability_scope import DIRECT_PARENT_TOOL_AUTHORITY_ATTR
    from agent_py_agent.agent.subagents.services.base import CreateRunParams
    from agent_py_agent.agent.tooling.models import ToolInvocationContext

    monkeypatch.setattr(lifecycle, 'auto_start_tasks',
        lambda _agent, _tasks, _params, **_kw: {'status': 'not_needed', 'run_ids': []})
    parent = agent.subagents.create_run(goal='真实完整父记录', role='coordinator',
        allowed_tools=['tool_search', 'create_subagents', 'read_file'])
    scope = {'schema_version': 'direct_parent_tool_authority.v1', 'parent_run_id': parent.id,
        'available_tool_names': ['tool_search', 'create_subagents', 'read_file']}
    if evidence is not None:
        scope['tool_grants'] = evidence
    task = agent.subagents.create_run(params=CreateRunParams(goal='旧创建快照恢复正式归一', thought='', plan=[],
        role='coordinator', parent_id=parent.id, root_id=parent.id, depth=1,
        allowed_tools=['tool_search', 'create_subagents', 'read_file'], attributes={DIRECT_PARENT_TOOL_AUTHORITY_ATTR: scope}))
    fresh = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home')), tmp_path)
    saved = fresh.subagents.load(task.id)
    assert 'list_tools' not in saved.allowed_tools
    assert ('tool_search:catalog' in saved.allowed_tools) is (evidence != ['tool_search'])
    context = fresh.subagents.runner_context.build_execution_context(task.id)
    snapshot = fresh.tools.runtime_snapshot(allowed_tools=context.allowed_tools, run_id=task.id)
    if evidence != ['tool_search']:
        assert_catalog_only(snapshot, tmp_path)
    item = {'goal': '旧创建快照的后代不得扩权'}
    if explicit:
        item['allowed_tools'] = ['tool_search', 'read_file']
    previous = set_current_subagent_context(fresh, run_id=task.id, task_attributes=saved.attributes)
    try:
        created = CreateSubagentsTool(fresh).execute_scoped({'items': [item]}, ToolInvocationContext(snapshot))
    finally:
        restore_current_subagent_context(fresh, previous)
    assert created.ok, created.output
    grand_id, = json.loads(created.output)['created_run_ids']
    restored = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home')), tmp_path)
    for run_id in (task.id, grand_id):
        stored = restored.subagents.load(run_id)
        assert 'list_tools' not in stored.allowed_tools
        assert ('tool_search:catalog' in stored.allowed_tools) is (evidence != ['tool_search'])
        context = restored.subagents.runner_context.build_execution_context(run_id)
        snapshot = restored.tools.runtime_snapshot(allowed_tools=context.allowed_tools, run_id=run_id)
        if evidence != ['tool_search']:
            assert_catalog_only(snapshot, tmp_path)
        else:
            runtime = snapshot.runtime('tool_search')
            assert runtime.model_spec.input_schema['properties']['action']['enum'] == ['search', 'catalog']
            call = ToolCall(call_id='legacy-full-' + run_id, tool_name='tool_search',
                arguments={'action': 'search', 'names': ['read_file']}, source_protocol='native',
                schema_hash=runtime.model_spec.schema_hash, run_id=run_id, turn_id='t', attempt_id='a')
            result = ToolExecutor().execute(ToolExecutorRequest(call=call, runtime_snapshot=snapshot,
                workspace_root=tmp_path, operation_store_required=False)).result
            assert result.ok, result.output
            assert json.loads(result.output)['loaded_for_next_model_call'] == ['read_file']


# LLM: 渲染真实开工合同与/help后只提取其规范catalog动作；经真实执行器执行，不用退役工具名或只测字符串存在。
# 函数用途: 防止面向模型和用户的帮助仍教list_tools，并证明推荐目录入口可执行。
@pytest.mark.parametrize('source', ['delivery', 'command_help'])
def test_tool_discovery_guidance_executes_catalog(agent, tmp_path, source):
    import re

    from agent_py_agent.agent.agent_core.delivery_contract_prompting import (
        render_delivery_contract_section,
    )
    from agent_py_agent.agent.command_catalog import CommandAccessContext, render_command_help

    if source == 'delivery':
        text = render_delivery_contract_section({'bootstrap_contract': {'materialization_targets': [{'workspace_relative_path': 'output/result.md'}]}})
    else:
        text = render_command_help(CommandAccessContext(surface='tui', is_admin=True, private_chat=True))
    assert 'list_tools' not in text
    actions = re.findall(r'tool_search\(action="([^"]+)"\)', text)
    assert actions == ['catalog']
    snapshot = agent.tools.runtime_snapshot()
    runtime = snapshot.runtime('tool_search')
    call = ToolCall(call_id=source, tool_name='tool_search', arguments={'action': actions[0]}, source_protocol='native',
        schema_hash=runtime.model_spec.schema_hash, run_id='guidance', turn_id='t', attempt_id='a')
    result = ToolExecutor().execute(ToolExecutorRequest(call=call, runtime_snapshot=snapshot,
        workspace_root=tmp_path, operation_store_required=False)).result
    assert result.ok, result.output


# LLM: 直接消费记忆目录领域投影的原始next_read，不依赖展示层重写补mode；实际读取可信当前canonical会话。
# 函数用途: 验证self_map历史归档指针是可执行的规范thread_browse参数。
def test_memory_directory_next_read_executes(tmp_path):
    from agent_py_agent.agent.capability.self_map_memory import memory_projection
    from agent_py_agent.tests.test_session_history_read import _owner

    owner = _owner(tmp_path)
    projection = memory_projection(SimpleNamespace(child=False, context_scope='owner', admin=False,
        snapshot=SimpleNamespace(available_tool_names={'session_search'}), agent=owner.agent))
    item, = projection.items
    next_read = item['next_read']
    assert next_read['tool'] == 'session_search'
    assert next_read['arguments'] == {'mode': 'thread_browse'}
    result = SessionSearchTool(owner.agent).execute(next_read['arguments'])
    assert result.ok, result.output
    assert json.loads(result.output)['messages']
