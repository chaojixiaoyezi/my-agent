"""普通 Agent/TUI 回合通过真实注册工具操作原业务库，不替换领域服务。"""
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.business_api import local_business_context
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.agent.tooling.models import ToolInvocationContext
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


@pytest.fixture
def agent(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    host = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(home),
        memory_path='memory.jsonl', execution_mode='local_unmanaged'), tmp_path / 'workspace')
    thread = host.conversation_store.threads.get_or_create({
        'canonical_user_id': 'local-agent', 'channel': 'tui', 'channel_conversation_id': 'business-test',
        'channel_user_id': 'local-agent', 'owner_id': host.home_paths.owner_id,
        'owner_home': str(host.home_paths.owner_home_dir)})
    host._current_run_params = RunParams(request_id='business-request',
        task_attributes={'conversation_thread_id': thread.thread_id})
    monkeypatch.setattr(host, 'run', lambda *a, **k: pytest.fail('业务管理不能调用模型'))
    return host


def business_call(agent, action, params=None, **options):
    if 'idempotency_key' in options:
        options['idempotency_key'] = 'business-test-' + options['idempotency_key']
    request = {'action': action, 'request_id': 'business-test', 'params': params or {}, **options}
    name = 'business_' + action.replace('.', '_')
    outcome = execute_registry_test_call(agent.tools, name, request, call_id='business-' + uuid4().hex)
    assert outcome.ok, outcome.output
    result = json.loads(outcome.output)
    assert result['error'] is None, result
    return result['data']


@pytest.mark.parametrize('query,prefix', [
    ('业务空间', 'business_spaces_'), ('space', 'business_spaces_'), ('spaces', 'business_spaces_'),
    ('后台代理', 'business_background_agents_'), ('常驻代理', 'business_background_agents_'),
    ('background_agents', 'business_background_agents_'), ('team', 'business_background_agents_'),
])
def test_real_tool_search_and_route_find_business_tools(agent, query, prefix):
    result = execute_registry_test_call(agent.tools, 'tool_search', {'query': query, 'limit': 5})
    assert result.ok, result.output
    loaded = result.metadata['handler_details']['tool_search']['loaded_tool_names']
    assert any(name.startswith(prefix) for name in loaded), result.output
    hits = agent.capability_router.search(query, kinds={'tool'}, limit=5)
    assert any(hit.card.name.startswith(prefix) for hit in hits), hits


@pytest.mark.parametrize('query,action', [
    ('业务空间 创建', 'spaces_create'), ('业务空间 列表', 'spaces_list'),
    ('后台代理 暂停', 'background_agents_suspend'), ('后台代理 恢复', 'background_agents_resume'),
    ('后台代理 发消息', 'background_agents_messages_send'), ('background_agents send_message', 'background_agents_messages_send'),
])
def test_action_words_find_executable_builtin_schema(agent, query, action):
    expected = 'business_' + action
    found = execute_registry_test_call(agent.tools, 'tool_search', {'query': query})
    assert found.ok, found.output
    assert expected in found.metadata['handler_details']['tool_search']['loaded_tool_names'], found.output
    assert expected in [hit.card.name for hit in agent.capability_router.search(query, kinds={'tool'}, limit=3)]


def test_self_map_overview_and_business_entries_explain_original_tools(agent):
    tool = agent.tools.tools['self_map']
    context = ToolInvocationContext(agent.tools.runtime_snapshot())
    overview = tool.execute_scoped({}, context)
    assert overview.ok, overview.output
    assert '业务空间与后台代理' in overview.output
    details = tool.execute_scoped({'section': 'business'}, context)
    assert details.ok, details.output
    for item in ('spaces', 'background_agents', '业务空间', '后台代理'):
        assert item in details.output
    entry = tool.execute_scoped({'section': 'business', 'item': 'background_agents'}, context)
    assert entry.ok and 'tool_search' in entry.output
    restricted = ToolInvocationContext(agent.tools.runtime_snapshot(allowed_tools=['self_map', 'tool_search']))
    limited = tool.execute_scoped({'section': 'business'}, restricted)
    assert limited.ok and 'business_background_agents_' not in limited.output


def test_workflows_space_scope_uses_same_owner_business_host(agent):
    space = business_call(agent, 'spaces.create', {'name': '研发空间'}, idempotency_key='space')
    sid = space.get('space_id') or space['space']['space_id']
    assert any(row['space_id'] == sid for row in business_call(agent, 'spaces.list')['items'])
    assert business_call(agent, 'spaces.get', {'space_id': sid})
    tool = agent.tools.tools['workflows']
    created = tool.execute({'action': 'workflows.create', 'idempotency_key': 'workflow',
        'definition': {'descriptor': {'id': 'daily', 'kind': 'workflow', 'name': '研发日报',
            'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}}, 'steps': []},
        'scope': {'scope_mode': 'spaces', 'space_ids': [sid]}})
    assert created.ok, created.output
    listed = execute_registry_test_call(agent.tools, 'workflows', {
        'action': 'workflows.list', 'space_id': sid, 'scope_mode': 'spaces'})
    assert listed.ok, listed.output
    rows = json.loads(listed.output)['items']
    assert [row['workflow_ref'] for row in rows] == ['daily']
    scoped = tool.execute({'action': 'workflows.scope.get', 'workflow_ref': 'daily'})
    assert scoped.ok and json.loads(scoped.output)['scope']['space_ids'] == [sid]
    from agent_py_agent.agent.assets.workflow_store import WorkflowStore

    store = WorkflowStore(agent.home_paths.owner_home_dir)
    before = store.get_workflow('daily').to_dict()
    agent.config.business_spaces_enabled = False
    denied = tool.execute({'action': 'workflows.list', 'space_id': sid})
    assert not denied.ok and denied.error_code == denied.reported_error_code == 'HOST_NOT_READY'
    # 已注册宿主写前拒绝保原码；关闭服务不修改已有工作流或其空间归属。
    payload = json.loads(denied.output)
    assert payload['error_code'] == payload['host_error']['code'] == 'HOST_NOT_READY'
    assert payload['host_error']['outcome'] == denied.effect_outcome == 'not_started'
    assert 'WORKFLOW_BUSINESS_HOST_UNAVAILABLE' not in denied.output
    assert store.get_workflow('daily').to_dict() == before


@pytest.mark.parametrize('scoped', [False, True])
def test_background_tools_use_real_identity_messages_and_lifecycle(agent, scoped):
    profile = {'model_backend': 'openai_compatible', 'model_name': 'unit-test-model',
        'api_base': 'https://example.test/v1', 'api_key': 'unit-test-only', 'model_context_window_tokens': 96000}
    profile_id = str(uuid4())
    execute_model_profile_operation(agent, 'add', {'profile_id': profile_id, 'profile': profile})
    execute_model_profile_operation(agent, 'set_default', {'profile_id': profile_id})
    sid = business_call(agent, 'spaces.create', {'name': '研发空间'}, idempotency_key='agent-space')['space_id'] if scoped else None
    created = business_call(agent, 'background_agents.create',
        {'name': '研发助手', 'space_id': sid}, idempotency_key='create-agent')['agent']
    identifier = created['agent_id']
    listed = business_call(agent, 'background_agents.list')
    assert [row['agent_id'] for row in listed['items']] == [identifier]
    assert business_call(agent, 'background_agents.get', {'agent_id': identifier})['agent']['name'] == '研发助手'
    revision = created['revision']
    for action, state in (('suspend', 'suspended'), ('resume', 'available')):
        changed = business_call(agent, 'background_agents.' + action, {'agent_id': identifier},
            expected_revision=revision, idempotency_key=action)['agent']
        assert changed['identity_state'] == state
        revision = changed['revision']
    sent = business_call(agent, 'background_agents.messages.send',
        {'agent_id': identifier, 'text': '请查收', 'input_kind': 'message'}, idempotency_key='message')
    assert sent
    assert business_call(agent, 'background_agents.messages.send',
        {'agent_id': identifier, 'text': '请查收', 'input_kind': 'message'}, idempotency_key='message') == sent
    retired = business_call(agent, 'background_agents.retire',
        {'agent_id': identifier, 'disposition': 'drain'}, expected_revision=revision, idempotency_key='retire')['agent']
    assert retired['identity_state'] in {'retired', 'retiring'}


def test_business_context_rechecks_turn_and_rejects_foreign_thread(agent):
    context, _ = local_business_context(agent, model=True)
    original = agent._current_run_params
    agent._current_run_params = replace(original, request_id='next-request')
    with pytest.raises(HostAPIError, match='CAPABILITY_DENIED'):
        context.check('spaces.list', {})
    foreign = agent.conversation_store.threads.create(owner_id='other', owner_home='/other')
    agent._current_run_params = replace(original, task_attributes={'conversation_thread_id': foreign.thread_id})
    outcome = agent.tools.tools['business_background_agents_list'].execute({
        'action': 'background_agents.list', 'request_id': 'foreign', 'params': {}})
    assert not outcome.ok
    assert outcome.error_code == outcome.reported_error_code == 'CHANNEL_ACTOR_SCOPE_MISMATCH'
    payload = json.loads(outcome.output)
    assert payload['error']['code'] == outcome.host_error['code'] == 'CHANNEL_ACTOR_SCOPE_MISMATCH'
    assert outcome.host_error['source'] == 'business_host' and outcome.host_error['http_status'] == 403
    assert payload['effect_outcome'] == outcome.effect_outcome == 'not_started'
    assert '请在原会话重新发起' in payload['error']['message']


def test_explicit_disable_rejects_existing_business_tool(agent):
    agent.config.business_spaces_enabled = False
    outcome = agent.tools.tools['business_spaces_list'].execute({'action': 'spaces.list',
        'request_id': 'disabled', 'params': {}})
    assert not outcome.ok and outcome.error_code == 'HOST_NOT_READY'


def test_local_list_authorization_has_no_per_record_source_reads(agent, monkeypatch):
    reads = []
    original = agent.conversation_store.threads.load_report

    def read(*args, **kwargs):
        reads.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(agent.conversation_store.threads, 'load_report', read)
    context, _ = local_business_context(agent, model=True)
    initial = len(reads)
    assert initial > 0
    for index in range(100):
        assert context.check('background_agents.list', {'agent_id': f'agent-{index}'})
    assert len(reads) == initial
    # 同一参数对象中的来源变化也必须失效，不能只比较 RunParams 对象身份。
    agent._current_run_params.task_attributes['conversation_thread_id'] = 'other-thread'
    with pytest.raises(HostAPIError, match='CAPABILITY_DENIED'):
        context.check('background_agents.list', {})


def test_native_health_discovery_and_call_keep_original_permission_boundary(agent):
    """新增宣告只使原只读方法可发现，不扩大当前工具范围或绕过原参数合同。"""
    name = 'business_workflows_health_get'
    found = execute_registry_test_call(agent.tools, 'tool_search', {'names': [name]})
    assert found.ok and name in found.metadata['handler_details']['tool_search']['loaded_tool_names']
    restricted = execute_registry_test_call(agent.tools, 'tool_search', {'names': [name]},
        allowed_tools=['tool_search'])
    assert restricted.ok
    assert name not in restricted.metadata['handler_details']['tool_search']['loaded_tool_names']
    request = {'action': 'workflows.health.get', 'request_id': 'health-model-read',
        'params': {'workflow_ref': 'model-health', 'limit': 1}}
    refused = execute_registry_test_call(agent.tools, name, request, allowed_tools=['tool_search'])
    assert not refused.ok and refused.handler_executed is False
    definition = {'descriptor': {'id': 'model-health', 'kind': 'workflow', 'name': '健康读取',
        'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}}, 'steps': []}
    business_call(agent, 'workflows.create', {'definition': definition}, idempotency_key='health-create')
    read = execute_registry_test_call(agent.tools, name, request)
    assert read.ok, read.output
    data = json.loads(read.output)['data']
    assert data['workflow_ref'] == 'model-health'
    assert data['health']['bounded_limit'] == 1 and data['health']['sampled_runs'] == 0
    assert data['health_read_coverage']['latest_guaranteed'] is False
    forged = execute_registry_test_call(agent.tools, name,
        {**request, 'params': {**request['params'], 'owner_id': 'local/other'}})
    assert not forged.ok and forged.handler_executed is False


def test_business_static_model_cache_matches_original_and_isolates_callers():
    from agent_py_agent.agent.gateway_parts import business_api as api
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_specs

    api._cached_business_model_spec.cache_clear()
    for spec in business_action_specs().values():
        expected = api._build_business_model_spec(spec)
        first = api._business_model_spec(spec)
        assert first == expected
        first.input_schema['properties'].clear()
        second = api._business_model_spec(spec)
        assert second == expected
        assert second is not first
        second.assert_schema_hash()
    assert api._cached_business_model_spec.cache_info().hits >= len(business_action_specs())


def test_business_static_model_cache_uses_complete_record_and_is_bounded():
    from agent_py_agent.agent.gateway_parts import business_api as api
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec

    api._cached_business_model_spec.cache_clear()
    original = business_action_spec('spaces.list')
    initial = api._business_model_spec(original)
    for index in range(140):
        record = original.metadata
        record['params_schema']['properties']['cache_marker'] = {'type': 'integer', 'const': index}
        changed = replace(original, _record_json=json.dumps(record, ensure_ascii=False))
        model = api._business_model_spec(changed)
        assert model.input_schema['properties']['params']['properties']['cache_marker']['const'] == index
        assert model != initial
    info = api._cached_business_model_spec.cache_info()
    assert info.maxsize == info.currsize == 128
    assert info.misses == 141
    api._cached_business_model_spec.cache_clear()


def test_business_mutable_extension_model_spec_is_not_cached():
    from agent_py_agent.agent.gateway_parts import business_api as api
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_spec

    original = v13_action_spec('sessions.space.get')
    params = {'type': 'object', 'properties': {'first': {'type': 'string'}}, 'additionalProperties': False}
    spec = replace(original, definition=replace(original.definition, params_schema=params))
    first = api._business_model_spec(spec)
    params['properties']['second'] = {'type': 'integer'}
    second = api._business_model_spec(spec)
    assert 'second' not in first.input_schema['properties']['params']['properties']
    assert 'second' in second.input_schema['properties']['params']['properties']
