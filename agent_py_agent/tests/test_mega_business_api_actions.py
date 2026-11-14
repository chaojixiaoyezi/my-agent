"""统一入口实际调用原W1存储；跨波缺失服务不视为集成完成。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_specs


@pytest.fixture
def host(tmp_path):
    return SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path / "alice", owner_id="local/user/alice",
        config_dir=tmp_path / "alice" / "config"),
        config=SimpleNamespace(business_spaces_enabled=True))


def invoke(host, action, params=None, **extra):
    context = api.bind_business_context(host, authorize=lambda action, facts: True)
    return api.execute_business({"action": action, "request_id": "request-one", "params": params or {}, **extra},
        context, api.BusinessServices(host))


def test_actual_space_store_create_read_idempotency_and_revision(host):
    request = {"action": "spaces.create", "request_id": "create-one", "params": {"name": "测试空间"},
               "idempotency_key": "create-space-key-0001"}
    context = api.bind_business_context(host, authorize=lambda action, facts: True)
    services = api.BusinessServices(host)
    first = api.execute_business(request, context, services)
    assert first[0] == 200
    data = first[1]["data"]
    # 原W1 create以revision=0开始；入口不能另增一次修订。
    assert data["name"] == "测试空间" and data["revision"] == 0
    assert api.execute_business(request, context, services)[1]["data"] == data
    get = invoke(host, "spaces.get", {"space_id": data["space_id"]})
    assert get[0] == 200
    detail = get[1]['data']
    for key in ('space_id', 'name', 'purpose', 'revision', 'state', 'mounts'):
        assert detail['space'][key] == data[key]
    assert data['allowed_actions'] and data['allowed_actions'] == detail['allowed_actions']
    assert data['controller_ref'] is None and data['controller_state'] == 'not_bound'
    conflict = invoke(host, "spaces.update", {"space_id": data["space_id"], "name": "改名"},
        idempotency_key="update-space-key-0001", expected_revision=999)
    assert conflict[0] == 409 and conflict[1]["error"]["code"] == "REVISION_CONFLICT"


@pytest.mark.parametrize("action", list(business_action_specs()))
def test_all_102_native_request_schemas_are_closed_before_domain(host, action):
    response = invoke(host, action, {"forged_owner": "someone-else"})
    assert response[0] == 400 and response[1]["error"]["code"] == "INVALID_SCHEMA"


def test_unissued_context_dict_or_copy_never_grants_authority(host):
    request = {"action": "system.capabilities", "request_id": "request-one", "params": {}}
    context = api.bind_business_context(host, authorize=lambda action, facts: True)
    for forged in ({"owner_id": host.home_paths.owner_id}, replace(context)):
        response = api.execute_business(request, forged, api.BusinessServices(host))
        assert response[0] == 403


def test_native_rechecks_current_scope_and_hides_other_owner(host):
    services = api.BusinessServices(host)
    allowed = [True]
    context = api.bind_business_context(host, authorize=lambda action, facts: allowed[0])
    request = {"action": "system.capabilities", "request_id": "request-one", "params": {}}
    assert api.execute_business(request, context, services)[0] == 200
    allowed[0] = False
    assert api.execute_business(request, context, services)[0] == 403
    bob = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=host.home_paths.owner_home_dir.parent / "bob",
        owner_id="local/user/bob"), config=host.config)
    other = api.bind_business_context(bob, authorize=lambda action, facts: True)
    assert api.execute_business(request, other, services)[0] == 404


def test_disabled_and_missing_domain_not_advertised(host):
    host.config.business_spaces_enabled = False
    status, result = invoke(host, "system.capabilities")
    assert status == 200
    assert "spaces.create" not in result["data"]["supported_actions"]
    assert "project.plan" not in result["data"]["supported_actions"]
    assert invoke(host, "spaces.list")[0] == 503


def test_model_profiles_only_whitelisted_fields_and_snapshot_cursor(host, monkeypatch):
    from agent_py_agent.agent.plugin_host.model_settings_v2_handlers import catalog
    monkeypatch.setattr(catalog, "read_model_profiles", lambda path: {"profiles": {
        "profile-one": {"model_name": "one", "provider_id": "provider-one", "api_key": "SECRET", "capability": "agentic"},
        "profile-two": {"model_name": "two", "provider_id": "provider-one", "capability": "embedding"}},
        "providers": {"provider-one": {"api_key": "SECRET", "base_url": "private"}}})
    status, first = invoke(host, "model_profiles.list", {"limit": 1})
    assert status == 200
    assert "SECRET" not in str(first) and "private" not in str(first)
    cursor = first["data"]["next_cursor"]
    assert cursor
    status, second = invoke(host, "model_profiles.list", {"limit": 1, "cursor": cursor})
    assert status == 200 and second["data"]["items"][0]["profile_id"] != first["data"]["items"][0]["profile_id"]


def test_public_entrypoints_registered_before_chat_fallback():
    from agent_py_agent.agent.command_catalog import COMMAND_INDEX, match_conversation_command
    from agent_py_agent.agent.gateway_parts.http_routes import match_gateway_http_route
    for name in ('space', 'team'):
        assert name in COMMAND_INDEX
        assert COMMAND_INDEX[name].submit_on_enter
        assert match_conversation_command('/' + name + ' list')[0] == name
    for path in ('/client/business', '/client/business/stream', '/client/business/artifacts'):
        assert match_gateway_http_route('POST', path) is not None
    assert match_gateway_http_route('GET', '/client/business/artifacts/art-one/content') is not None


def test_frozen_dispatch_signature_is_same_shared_domain(host):
    dispatch = getattr(api, 'dispatch_business_action', None)
    assert callable(dispatch)
    result = dispatch({'action': 'system.capabilities', 'request_id': 'request-one', 'params': {}},
        authenticated_context=api.bind_business_context(host, authorize=lambda action, facts: True),
        services=api.BusinessServices(host))
    assert result['state'] == 'completed' and result['error'] is None


@pytest.fixture
def native_http(tmp_path):
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.home_layout import home_paths
    root = tmp_path / 'native-home'
    base = SimpleNamespace(home_paths=home_paths(root), config=AgentConfig(business_spaces_enabled=True))
    server = GatewayHTTPServer(0, gateway_paths_from_root(root / 'gateway'), params=GatewayHTTPServerParams(agent=base))
    server.start()
    try:
        yield server, base
    finally:
        server.stop()


def native_request(native_http, method, path, data=None, *, user='business-alice', headers=None, raw=None):
    import http.client
    import json
    server, _ = native_http
    conn = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=10)
    request_headers = {'X-Gateway-Token': server._local_client_credential, 'X-User-Id': user,
        'X-Channel': 'feishu', 'Content-Type': 'application/json', **(headers or {})}
    body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
    try:
        conn.request(method, path, body, request_headers)
        response = conn.getresponse()
        content = response.read()
        return response.status, dict(response.getheaders()), content
    finally:
        conn.close()


def test_native_http_real_auth_owner_a_b_and_csrf(native_http):
    import json
    request = {'action': 'spaces.create', 'request_id': 'create-one', 'params': {'name': '本人空间'},
        'idempotency_key': 'native-create-key-one'}
    status, _, raw = native_request(native_http, 'POST', '/client/business', request)
    assert status == 200, raw
    space_id = json.loads(raw)['data']['space_id']
    get = {'action': 'spaces.get', 'request_id': 'get-one', 'params': {'space_id': space_id}}
    assert native_request(native_http, 'POST', '/client/business', get, user='business-bob')[0] == 404
    assert native_request(native_http, 'POST', '/client/business', get)[0] == 200
    assert native_request(native_http, 'POST', '/client/business', get, headers={'X-Gateway-Token': ''})[0] == 403
    assert native_request(native_http, 'POST', '/client/business', get, headers={'Origin': 'https://other.invalid'})[0] == 403


def test_owner_workflow_maintenance_no_index_is_read_only(host):
    from agent_py_agent.agent.user_space import owner_maintenance
    maintain = getattr(owner_maintenance, 'run_owner_workflow_maintenance', None)
    assert callable(maintain)
    before = list(host.home_paths.owner_home_dir.parent.rglob('*'))
    assert maintain(host.home_paths, {}, now=1000, safe_to_delete=False) == {'executed': False, 'reason': 'retention_not_safe'}
    assert maintain(host.home_paths, {}, now=1000, safe_to_delete=True) == {'executed': False, 'reason': 'no_workflow_index'}
    assert list(host.home_paths.owner_home_dir.parent.rglob('*')) == before


def test_post_commit_revocation_keeps_committed_outcome_and_never_repeats(host, monkeypatch):
    services = api.BusinessServices(host)
    allowed, calls = [True], []
    context = api.bind_business_context(host, authorize=lambda action, facts: allowed[0])
    original = services.invoke

    def revoke_after_commit(*args, **kwargs):
        calls.append(args[0])
        result = original(*args, **kwargs)
        allowed[0] = False
        return result

    monkeypatch.setattr(services, 'invoke', revoke_after_commit)
    status, result = api.execute_business({'action': 'spaces.create', 'request_id': 'revoke-after-write',
        'params': {'name': '已提交空间'}, 'idempotency_key': 'post-commit-revocation-key'}, context, services)
    assert status == 403
    assert result['effect_outcome'] == 'committed'
    assert calls == ['spaces.create']
    assert invoke(host, 'spaces.list')[1]['data']['items'][0]['name'] == '已提交空间'


def test_cold_cli_space_create_and_get_use_original_store(tmp_path, monkeypatch, capsys):
    import json

    from agent_py_agent.agent import settings
    from agent_py_agent.agent.conversation.business_access import BusinessError
    from agent_py_agent.agent.user_space import home_layout
    from agent_py_agent.cli.parser import _cmd_business
    paths = home_layout.home_paths(tmp_path / 'cli-home')
    monkeypatch.setattr(home_layout, 'home_paths', lambda: paths)
    monkeypatch.setattr(settings, 'load_config', lambda path: settings.AgentConfig(business_spaces_enabled=True))
    args = SimpleNamespace(config='unused', business_namespace='space', business_args=['create', '命令行空间'])
    try:
        code = _cmd_business(args)
    except BusinessError as error:
        code = error.code
    assert code == 0
    created = json.loads(capsys.readouterr().out)['data']
    args.business_args = ['get', created['space_id']]
    assert _cmd_business(args) == 0
    assert json.loads(capsys.readouterr().out)['data']['space']['name'] == '命令行空间'


def test_native_cache_follows_live_feature_enable(native_http):
    _, base = native_http
    base.config.business_spaces_enabled = False
    assert native_request(native_http, 'POST', '/client/business',
        {'action': 'system.capabilities', 'request_id': 'disabled-read', 'params': {}})[0] == 200
    base.config.business_spaces_enabled = True
    status, _, raw = native_request(native_http, 'POST', '/client/business',
        {'action': 'spaces.create', 'request_id': 'enabled-create', 'params': {'name': '开关重启后'},
         'idempotency_key': 'live-feature-enable-key'})
    assert status == 200, raw


@pytest.mark.parametrize('reference', ['none', 'real_subworkflow', 'unreadable_source'])
def test_owner_maintenance_uses_real_w3_index_and_preserves_unknown_references(host, reference):
    from datetime import datetime, timezone

    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_workflow_maintenance
    from agent_py_agent.tests.test_business_v12_f4_workflows import definition
    services = api.BusinessServices(host)
    services.workflows.clock = lambda: 100.0
    context = api.bind_business_context(host, authorize=lambda action, facts: True)
    actor = services.actor(context)
    services.workflows.execute('workflows.create', {'definition': definition(),
        'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, actor=actor, idempotency_key='seed-orphan')
    # 未登记引用的空工作流应回收；真实父定义才是引用权威，缺reader不是引用夹具。
    if reference != 'none':
        services.workflows.execute('workflows.create', {'definition': definition('real-parent', steps=[{
            'step_id': 'child', 'name': '真实引用', 'mode': 'subworkflow', 'subworkflow_ref': 'workflow:daily'}]),
            'scope': {'scope_mode': 'owner_global', 'space_ids': []}}, actor=actor,
            idempotency_key='seed-real-parent')
    if reference == 'unreadable_source':
        (host.home_paths.owner_home_dir / 'workflows/real-parent/workflow.json').write_text('{broken source')
    now = datetime.fromtimestamp(40 * 86400, timezone.utc)
    result = run_owner_workflow_maintenance(host.home_paths, {}, now=now, safe_to_delete=True)
    assert result['executed'] is True
    expected = {'recycled': 0, 'skipped': 0, 'failed': 0, 'unknown': 0}
    expected[{'none': 'recycled', 'real_subworkflow': 'skipped', 'unreadable_source': 'unknown'}[reference]] = 1
    assert result['counts'] == expected
    assert run_owner_workflow_maintenance(host.home_paths, {}, now=now, safe_to_delete=True) == result
    current = services.workflows.execute('workflows.get', {'workflow_ref': 'daily'}, actor=actor)
    assert current['workflow']['lifecycle'] == ('recycled' if reference == 'none' else 'active')
    assert services.workflows.store.get_workflow('daily').descriptor.revision == (2 if reference == 'none' else 1)


def test_actual_core_registry_reaches_shared_business_api_and_rejects_alias(tmp_path):
    import json

    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    agent = SimpleAgent(AgentConfig(enable_tools=True, business_spaces_enabled=True, memory_path='memory.jsonl'), tmp_path)
    tool = agent.tools.tools['business_system_capabilities']
    response = tool.execute({'action': 'system.capabilities', 'request_id': 'model-one', 'params': {}})
    assert json.loads(response.output)['error'] is None
    denied = tool.execute({'action': 'approvals.decide', 'request_id': 'model-fake', 'params': {}})
    # 工具错误码沿原taxonomy归一；业务原码必须保留在reported字段及JSON正文。
    assert denied.reported_error_code == 'INVALID_SCHEMA'
    assert json.loads(denied.output)['error']['code'] == 'INVALID_SCHEMA'
    scope_tool = agent.tools.tools['business_workflows_scope_set']
    duplicate = scope_tool.execute({'action': 'workflows.scope.set', 'request_id': 'duplicate-scope',
        'params': {'workflow_ref': 'daily', 'scope': {'scope_mode': 'spaces', 'space_ids': ['s1', 's1']}},
        'expected_revision': 0, 'idempotency_key': 'duplicate-scope-key'})
    assert duplicate.reported_error_code == 'INVALID_SCHEMA'
    assert json.loads(duplicate.output)['error']['code'] == 'INVALID_SCHEMA'
    context, _ = api.local_business_context(agent, model=True)
    with pytest.raises(api.HostAPIError) as human:
        context.check('approvals.decide', {})
    assert human.value.code == 'CAPABILITY_DENIED'


@pytest.fixture
def background_http(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig

    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'background-home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=False,
        business_spaces_enabled=True, memory_path='memory.jsonl'), tmp_path / 'workspace')
    server = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / 'gateway'),
        params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        yield server, agent
    finally:
        server.stop()


def background_call(server, action, params=None, *, user='business-alice', **extra):
    import json
    if 'idempotency_key' in extra:
        extra['idempotency_key'] = 'background-wire-' + extra['idempotency_key']
    status, _, raw = native_request(server, 'POST', '/client/business', {
        'action': action, 'request_id': 'native-' + action, 'params': params or {}, **extra}, user=user)
    return status, json.loads(raw)


def create_background(server, *, user='business-alice'):
    status, space = background_call(server, 'spaces.create', {'name': '后台空间'},
        user=user, idempotency_key='wire-space')
    assert status == 200, space
    params = {'space_id': space['data']['space_id'], 'name': '原后台身份'}
    status, result = background_call(server, 'background_agents.create', params,
        user=user, idempotency_key='wire-background')
    assert status == 200, result
    return params, result['data']['agent']


def test_native_background_real_service_replay_and_owner_store(background_http):
    from agent_py_agent.agent.gateway_parts.owner_conversation_store import owner_conversation_store

    params, agent = create_background(background_http)
    replay = background_call(background_http, 'background_agents.create', params,
        idempotency_key='wire-background')
    assert replay[0] == 200 and replay[1]['data']['agent'] == agent
    status, fetched = background_call(background_http, 'background_agents.get', {'agent_id': agent['agent_id']})
    assert status == 200, fetched
    assert agent['agent_id'] == agent['thread_id'] == fetched['data']['agent']['thread_id']
    assert fetched['data']['current_execution'] is None
    server, base = background_http
    [services] = [value for value in server._business_services.values()
                  if value.owner_id.endswith('/business-alice')]
    original = owner_conversation_store(base, services.host.home_paths, initialize=False)
    stored = original.threads.require(agent['thread_id'])
    assert stored.owner_id == services.owner_id
    assert stored.metadata['background_agent']['space_id'] == params['space_id']
    assert services.host.conversation_store.storage.root == original.storage.root
    assert base.conversation_store.threads.load(agent['thread_id']) is None


def test_native_background_owner_a_b_and_cached_actor_are_not_shared(background_http):
    _, alice = create_background(background_http)
    _, bob = create_background(background_http, user='business-bob')
    assert alice['agent_id'] != bob['agent_id']
    status, response = background_call(background_http, 'background_agents.get',
        {'agent_id': alice['agent_id']}, user='business-bob')
    assert status == 404, response
    assert '原后台身份' not in str(response)
    assert background_call(background_http, 'background_agents.get', {'agent_id': alice['agent_id']})[0] == 200
    assert background_call(background_http, 'background_agents.get', {'agent_id': bob['agent_id']},
        user='business-bob')[0] == 200


def test_native_background_update_lifecycle_and_controller_use_original_methods(background_http):
    _, agent = create_background(background_http)
    identifier = agent['agent_id']
    status, updated = background_call(background_http, 'background_agents.update',
        {'agent_id': identifier, 'name': '新名字'}, expected_revision=agent['revision'], idempotency_key='wire-update')
    assert status == 200, updated
    revision = updated['data']['agent']['revision']
    for action, expected in [('suspend', 'suspended'), ('resume', 'available'),
                             ('retire', 'retired'), ('restore', 'available')]:
        params = {'agent_id': identifier, **({'disposition': 'drain'} if action == 'retire' else {})}
        status, result = background_call(background_http, 'background_agents.' + action, params,
            expected_revision=revision, idempotency_key='wire-' + action)
        assert status == 200, result
        assert result['data']['agent']['identity_state'] == expected
        revision = result['data']['agent']['revision']
    status, controller = background_call(background_http, 'controllers.ensure', idempotency_key='wire-controller')
    assert status == 200, controller
    status, fetched = background_call(background_http, 'controllers.get')
    assert status == 200 and fetched['data']['thread_id'] == controller['data']['thread_id']
    assert fetched['data']['available'] is True


def test_native_background_defaults_do_not_accept_invalid_explicit_model(background_http, monkeypatch):
    from agent_py_agent.agent.settings import thread_model_selection

    monkeypatch.setattr(thread_model_selection, 'default_model_profile_id', lambda host: '')
    params, agent = create_background(background_http)
    assert agent['model_profile_id'] == 'default'
    status, model = background_call(background_http, 'background_agents.model.get', {'agent_id': agent['agent_id']})
    assert status == 200 and model['data']['model_profile_id'] == 'default'
    status, denied = background_call(background_http, 'background_agents.create',
        {**params, 'model_profile_id': 'missing-profile'}, idempotency_key='wire-invalid-profile')
    assert status >= 400 and denied['error'] is not None
    status, listed = background_call(background_http, 'background_agents.list')
    assert status == 200 and [row['agent_id'] for row in listed['data']['items']] == [agent['agent_id']]


@pytest.mark.parametrize('enabled', [True, False])
def test_background_auto_wiring_requires_real_store_and_enabled_spaces(host, enabled):
    from agent_py_agent.agent.conversation.store import ConversationStore

    host.config.business_spaces_enabled = enabled
    bare = api.BusinessServices(host)
    assert bare.background is None
    assert bare.ready('background_agents.create') is False
    host.conversation_store = ConversationStore(host.home_paths.owner_home_dir / 'conversations')
    wired = api.BusinessServices(host)
    assert (wired.background is not None) is enabled
    assert wired.ready('background_agents.create') is enabled


def test_native_background_task_routes_freeze_one_canonical_business_context(background_http):
    params, agent = create_background(background_http)
    request = {'agent_id': agent['agent_id'], 'space_id': params['space_id'], 'instruction': '原始任务'}
    status, assigned = background_call(background_http, 'tasks.assign', request, idempotency_key='assign-task')
    assert status == 200, assigned
    task_id = assigned['data']['task_id']
    assert background_call(background_http, 'tasks.assign', request,
        idempotency_key='assign-task')[1]['data']['task_id'] == task_id
    server, _ = background_http
    [services] = server._business_services.values()
    task = services.host.conversation_store.session_tasks.require(task_id)
    assert task.metadata['business_context']['task_id'] == task_id
    assert task.metadata['business_context']['space_id'] == params['space_id']
    status, followed = background_call(background_http, 'tasks.follow_up',
        {'task_id': task_id, 'instruction': '补充任务'}, idempotency_key='follow-up-task')
    assert status == 200, followed
    assert followed['data']['prior_task_ref'] == task_id
    followed_task = services.host.conversation_store.session_tasks.require(followed['data']['new_task_id'])
    status, cancelled = background_call(background_http, 'tasks.cancel',
        {'task_id': followed_task.task_id, 'include_descendants': False}, idempotency_key='cancel-task',
        expected_revision=int(followed_task.updated_at * 1000000))
    assert status == 200, cancelled
    status, submitted = background_call(background_http, 'tasks.submit',
        {'space_id': params['space_id'], 'instruction': '总控任务'}, idempotency_key='submit-task')
    assert status == 200, submitted
    submitted_task = services.host.conversation_store.session_tasks.require(submitted['data']['task_id'])
    assert submitted_task.metadata['business_context']['task_id'] == submitted_task.task_id


def test_background_port_requires_current_issued_context_and_same_owner(host):
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    from agent_py_agent.agent.conversation.store import ConversationStore

    host.conversation_store = ConversationStore(host.home_paths.owner_home_dir / 'conversations')
    source = host.conversation_store.threads.create(owner_id=host.home_paths.owner_id)
    actor = HostChannelActor('another-owner', source.thread_id, 'request', 'feishu', '', 'person')
    services = api.BusinessServices(host)
    context = api.bind_business_context(host, authorize=lambda action, facts: True, actor=actor)
    for candidate in (None, replace(context)):
        with pytest.raises(api.BusinessError) as denied:
            services.background.execute('background_agents.list', {}, actor=actor, context=candidate)
        assert denied.value.code == 'ACTOR_REQUIRED'
    with pytest.raises(api.BusinessError) as denied:
        services.background.execute('background_agents.list', {}, actor=actor, context=context)
    assert denied.value.code == 'BUSINESS_ACCESS_DENIED'


def observed_native_task(native, *, user='business-alice'):
    """经真实HTTP提交普通任务，后续观察不替换任何领域/流生产者。"""
    params, agent = create_background(native, user=user)
    status, assigned = background_call(native, 'tasks.assign', {'agent_id': agent['agent_id'],
        'space_id': params['space_id'], 'instruction': '原生产任务'}, user=user, idempotency_key='observe-task')
    assert status == 200, assigned
    [services] = [item for item in native[0]._business_services.values() if item.owner_id.endswith('/' + user)]
    return services, services.host.conversation_store.session_tasks.require(assigned['data']['task_id'])


def seed_observed_runtime(services, task):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path

    runtime = RuntimeRepository(runtime_db_path(services.owner_home))
    run = runtime.record_run_creation(owner_id=services.owner_id, run_id='observed-' + task.task_id,
        thread_id=task.target_thread_id, task_run_metadata={'business_context': task.metadata.get('business_context'),
            'session_task_ref': task.task_id})
    return runtime, run


def open_native_observation(native, request, *, headers=None):
    import http.client
    import json

    server, _ = native
    conn = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=20)
    conn.request('POST', '/client/business/stream', json.dumps(request).encode(), {
        'X-Gateway-Token': server._local_client_credential, 'X-User-Id': 'business-alice',
        'X-Channel': 'feishu', 'Content-Type': 'application/json', **(headers or {})})
    return conn, conn.getresponse()


def test_observation_native_task_list_detail_owner_and_missing_db(background_http):
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path

    services, task = observed_native_task(background_http)
    db = runtime_db_path(services.owner_home)
    assert not db.exists()
    status, listed = background_call(background_http, 'tasks.list')
    assert status == 200, listed
    assert [item['task_id'] for item in listed['data']['items']] == [task.task_id]
    status, detail = background_call(background_http, 'tasks.get', {'task_id': task.task_id})
    assert status == 200, detail
    assert detail['data']['workflow_ref'] is None
    assert detail['data']['assignments'][0]['task_id'] == task.task_id
    assert not db.exists(), '只读观察不得创建缺失运行库'
    bob, bob_task = observed_native_task(background_http, user='business-bob')
    status, foreign = background_call(background_http, 'tasks.get', {'task_id': bob_task.task_id})
    assert status == 404, foreign
    assert bob_task.task_id not in str(background_call(background_http, 'tasks.list')[1]['data'])
    assert services.projection is services.observation
    assert services.observation is not bob.observation
    assert services.observation.store is services.host.conversation_store
    assert services.observation.runtime.db_path == db
    assert services.observation.runtime.read_only and not services.observation.runtime.existing_only


def test_observation_native_stream_decodes_once_and_reconnects(background_http):
    import json

    services, task = observed_native_task(background_http)
    runtime, run = seed_observed_runtime(services, task)

    def append():
        return runtime.append_event(event_type='attempt.progress', attempt_id=run['attempt_id'],
            agent_run_id=run['agent_run_id'], task_run_id=run['task_run_id'])

    event = append()
    cursor = None
    for expected in (event, None):
        if expected is None:
            expected = append()
        conn, response = open_native_observation(background_http, {'request_id': 'observe-native', 'filter': {},
            **({'cursor': cursor} if cursor else {})})
        try:
            assert response.status == 200, response.read()
            assert response.getheader('Content-Type').startswith('application/x-ndjson')
            snapshot = json.loads(response.readline())
            assert isinstance(snapshot, dict) and snapshot['stream_item'] == 'snapshot'
            assert snapshot['payload']['tasks'][0]['task_id'] == task.task_id
            for _ in range(8):
                frame = json.loads(response.readline())
                assert isinstance(frame, dict)
                if frame['event_id'] == expected['event_id']:
                    break
            else:
                pytest.fail('真实运行事件没有从原生HTTP流读回')
            assert frame['run_id'] == 'observed-' + task.task_id and frame['attempt_id'] == run['attempt_id']
            cursor = frame['cursor']
        finally:
            response.close()
            conn.close()
    assert services.host.conversation_store.session_tasks.require(task.task_id).status == task.status


@pytest.mark.parametrize('extra,headers,status', [
    ({'forged_owner': 'other'}, {}, 400),
    ({}, {'Origin': 'https://other.invalid'}, 403),
    ({}, {'X-Gateway-Token': ''}, 403),
])
def test_observation_native_stream_keeps_closed_schema_and_auth(background_http, extra, headers, status):
    conn, response = open_native_observation(background_http,
        {'request_id': 'observe-denied', 'filter': {}, **extra}, headers=headers)
    try:
        assert response.status == status
    finally:
        response.close()
        conn.close()


def test_observation_composed_actor_is_distinct_live_and_service_bound(tmp_path):
    from agent_py_agent.agent.conversation.business_access import require_actor
    from agent_py_agent.tests.test_business_v12_f7_observation import fixture

    case = fixture(tmp_path)
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_id=case.owner, owner_home_dir=tmp_path),
        config=SimpleNamespace(business_spaces_enabled=True), conversation_store=case.store)
    services = api.BusinessServices(host, projection=case.service)
    context = api.bind_business_context(host, subject_id='current-subject', authorize=lambda _a, _f: case.allowed[0])
    status, result = api.execute_business({'request_id': 'observe-actor', 'action': 'tasks.list', 'params': {}}, context, services)
    assert status == 200, result
    first, second = services.observation_actor(context), services.observation_actor(context)
    assert first is not second and first.subject_id == 'current-subject'
    assert require_actor(first, case.service) is first
    assert require_actor(services.actor(context), services.spaces).service is services.spaces
    with pytest.raises(api.BusinessError):
        require_actor(context, case.service)
    case.allowed[0] = False
    with pytest.raises(api.HostAPIError) as denied:
        case.service.query('tasks.list', {}, actor=first)
    assert denied.value.code == 'CAPABILITY_DENIED'


def test_observation_optional_detail_readiness_is_not_a_generic_query(tmp_path):
    from agent_py_agent.agent.conversation.business_observability import BusinessObservationService

    host = SimpleNamespace(home_paths=SimpleNamespace(owner_id='local/test', owner_home_dir=tmp_path),
        config=SimpleNamespace(business_spaces_enabled=True))
    service = BusinessObservationService(tmp_path, owner_id=host.home_paths.owner_id)
    composed = api.BusinessServices(host, projection=service)
    for action in ('logs.read', 'tool_calls.get', 'artifacts.get', 'approvals.get', 'permissions.get', 'changes.get', 'usage.get', 'operations.get'):
        assert not composed.ready(action), action


@pytest.fixture
def dispatch_case(host):
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.settings import AgentConfig

    host.config = AgentConfig(model_backend='echo', business_spaces_enabled=True)
    host.conversation_store = ConversationStore(host.home_paths.owner_home_dir / 'conversations')
    source = host.conversation_store.threads.create(owner_id=host.home_paths.owner_id,
        owner_home=str(host.home_paths.owner_home_dir))
    actor = HostChannelActor(host.home_paths.owner_id, source.thread_id, 'original-request',
        'plugin', 'original-installation', 'original-actor')
    context = api.bind_business_context(host, authorize=lambda _a, _f: True, actor=actor)
    services = api.BusinessServices(host)
    status, space = api.execute_business({'action': 'spaces.create', 'request_id': 'dispatch-space',
        'params': {'name': '派发空间'}, 'idempotency_key': 'dispatch-space-key'}, context, services)
    assert status == 200, space
    params = {'space_id': space['data']['space_id'], 'name': '原派发目标'}
    created = services.background.execute('background_agents.create', params, actor=actor,
        context=context, idempotency_key='dispatch-agent-key')
    target = host.conversation_store.threads.require(created['agent']['thread_id'])
    return services, context, actor, target, params['space_id']


def test_business_dispatch_transports_original_post_publication_uncertainty(dispatch_case, monkeypatch):
    from agent_py_agent.agent.conversation import session_task_dispatch as dispatch

    services, context, actor, target, space = dispatch_case
    outcomes = []
    original = dispatch.dispatch_session_task

    def fail_wake(*_args, **_kwargs):
        raise OSError('post-publication failure')

    def record(*args, **kwargs):
        result = original(*args, **kwargs)
        outcomes.append(result)
        return result

    monkeypatch.setattr(dispatch, '_maybe_wake', fail_wake)
    monkeypatch.setattr(dispatch, 'dispatch_session_task', record)
    service = api._BusinessBackgroundService(services, context)
    with pytest.raises(dispatch.SessionTaskDispatchError) as denied:
        service._dispatch_locked({'space_id': space, 'instruction': '只派发一次'}, target,
            actor=actor, idempotency_key='original-dispatch-key', origin_task_id='')
    assert len(outcomes) == 1
    assert denied.value.outcome is outcomes[0]
    assert denied.value.error_code == outcomes[0].error_code == 'TOOL_EXECUTION_FAILED'
    assert denied.value.effect_outcome == outcomes[0].effect_outcome == 'unknown'
    identity = outcomes[0].result_envelope['session_task_dispatch']
    task = services.host.conversation_store.session_tasks.require(identity['task_id'])
    assert identity == {'task_id': task.task_id, 'sender_thread_id': actor.source_thread_id,
        'target_thread_id': target.thread_id, 'dedupe_key': task.dedupe_key}
    assert services.host.conversation_store.session_tasks.list_report(limit=0)[0] == [task]


@pytest.mark.parametrize('effect', ['not_started', 'unknown'])
def test_business_dispatch_error_projection_keeps_structured_effect_and_closed_schema(dispatch_case, monkeypatch, effect):
    from agent_py_agent.agent.conversation import session_task_dispatch as dispatch
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

    services, context, actor, target, space = dispatch_case
    outcome = ToolHandlerOutcome('create_session_task', False, 'not an exception-text protocol',
        error_code='TOOL_EXECUTION_FAILED', effect_outcome=effect,
        result_envelope={'session_task_dispatch': {'sender_thread_id': actor.source_thread_id,
            'target_thread_id': target.thread_id, 'dedupe_key': 'original-dedupe'}})
    calls = []

    def failed(*args, **kwargs):
        calls.append(args[1])
        return outcome

    monkeypatch.setattr(dispatch, 'dispatch_session_task', failed)
    status, result = api.execute_business({'action': 'tasks.assign', 'request_id': 'original-request',
        'params': {'agent_id': target.thread_id, 'space_id': space, 'instruction': '不重发'},
        'idempotency_key': 'original-dispatch-key'}, context, services)
    assert status == 503, result
    assert result['effect_outcome'] == effect
    assert result['error']['code'] == 'TOOL_EXECUTION_FAILED'
    assert len(calls) == 1 and calls[0].sender_thread_id == actor.source_thread_id
    assert set(result) == {'request_id', 'operation_id', 'state', 'data', 'error',
        'allowed_actions', 'warnings', 'effect_outcome'}
    assert result['request_id'] == actor.entry_request_id and result['data'] == {}
    assert set(result['error']) == {'code', 'message', 'retryable', 'details'}
    assert 'session_task_dispatch' not in str(result)


@pytest.mark.parametrize('state', ['retiring', 'retired'])
def test_retired_background_message_rejected_before_enqueue_and_followup_read_continues(dispatch_case, monkeypatch, state):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.background_team_lifecycle import background_identity

    services, context, actor, target, _ = dispatch_case
    identity = background_identity(target)
    services.host.conversation_store.threads.update_atomic(target.thread_id, lambda current:
        replace(current, metadata={**current.metadata, 'background_agent': replace(identity, identity_state=state).to_dict()}))
    def no_enqueue(*args, **kwargs):
        pytest.fail('retired target reached message enqueue')
    monkeypatch.setattr(services.host.conversation_store.guidance, 'append_once', no_enqueue)
    status, result = api.execute_business({'request_id': 'retired-message', 'action': 'background_agents.messages.send',
        'params': {'agent_id': target.thread_id, 'text': '不应发送', 'input_kind': 'message'},
        'idempotency_key': 'retired-message-key'}, context, services)
    assert status == 409, result
    assert result['effect_outcome'] == 'not_started' and result['error']['code'] == 'AGENT_NOT_AVAILABLE'
    details = {row['key']: row['value'] for row in result['error']['details']}
    assert details['identity_state'] == state and details['next_action']
    status, read = api.execute_business({'request_id': 'after-rejection', 'action': 'background_agents.list',
        'params': {}}, context, services)
    assert status == 200, read


def test_background_message_post_enqueue_failure_stays_unknown(dispatch_case, monkeypatch):
    services, context, actor, target, _ = dispatch_case
    def fail_wake(*args, **kwargs):
        raise OSError('synthetic private detail')
    monkeypatch.setattr(services.host.conversation_store.wakes, 'raise_signal', fail_wake)
    status, result = api.execute_business({'request_id': 'uncertain-message', 'action': 'background_agents.messages.send',
        'params': {'agent_id': target.thread_id, 'text': '已入队后异常', 'input_kind': 'message'},
        'idempotency_key': 'uncertain-message-key'}, context, services)
    assert status == 503 and result['effect_outcome'] == 'unknown', result
    assert result['error']['code'] == 'OUTCOME_UNKNOWN'
    details = {row['key']: row['value'] for row in result['error']['details']}
    assert details['stage'] == 'invoke' and details['exception_type'] == 'OSError'
    assert details['retry_same_request'] is False
    assert 'request_id' in details['next_action'] and '勿重新' in result['error']['message']
    assert 'synthetic private detail' not in str(result)


def test_business_input_rejection_returns_correct_nested_path_on_wire(background_http):
    status, result = background_call(background_http, 'tasks.list', {'include_completed': 'true'})
    assert status == 400 and result['effect_outcome'] == 'not_started', result
    details = {row['key']: row['value'] for row in result['error']['details']}
    assert details['field_path'] == '$.params.include_completed'
    assert details['suggested_path'] == '$.params.filter.include_completed'
    assert details['actual_type'] == 'string' and details['expected_type'] == 'boolean'
    assert 'true' in details['next_action']
    status, result = background_call(background_http, 'tasks.list', {'filter': {'include_completed': True}})
    assert status == 200 and result['state'] == 'completed', result


def test_business_response_validation_preserves_published_effect(host, monkeypatch):
    context = api.bind_business_context(host, authorize=lambda action, facts: True)
    services = api.BusinessServices(host)
    invoke_action = services.invoke

    def malformed_receipt(*args, **kwargs):
        invoke_action(*args, **kwargs)
        return {'unknown_field': 'synthetic-private-value'}

    monkeypatch.setattr(services, 'invoke', malformed_receipt)
    status, result = api.execute_business({'action': 'spaces.create', 'request_id': 'original-request',
        'idempotency_key': 'original-result-key', 'params': {'name': '已写入的空间'}}, context, services)
    assert status == 400 and result['effect_outcome'] == 'committed', result
    assert '结果未通过' in result['error']['message']
    details = {row['key']: row['value'] for row in result['error']['details']}
    assert '勿重新' in details['next_action'] and '未开始执行' not in str(result)
    assert 'synthetic-private-value' not in str(result)


@pytest.mark.parametrize("owner_field", ["actor", "target", "source"])
def test_background_message_owner_denial_is_prewrite(dispatch_case, monkeypatch, owner_field):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.business_access import BusinessError
    from agent_py_agent.agent.conversation.session_messaging import (
        _queue_owned_background_message_locked,
    )

    services, _context, actor, target, _space = dispatch_case
    if owner_field == "actor":
        actor = replace(actor, owner_id="local/other")
    elif owner_field == "target":
        target = replace(target, owner_id="local/other")
    else:
        services.host.conversation_store.threads.update_atomic(actor.source_thread_id,
            lambda current: replace(current, owner_id="local/other"))
    monkeypatch.setattr(services.host.conversation_store.guidance, "append_once",
        lambda *a, **kw: pytest.fail("authorization denial reached enqueue"))
    with pytest.raises(BusinessError) as denied:
        _queue_owned_background_message_locked(services.host, actor=actor, target=target,
            text="not delivered", operation_key="denied-owner", input_kind="message")
    assert api._error_code(denied.value) == ("BUSINESS_ACCESS_DENIED", 403, "not_started")
    assert denied.value.details["next_action"]


@pytest.mark.parametrize("missing", [False, True])
def test_business_child_authorization_denial_is_prewrite(dispatch_case, monkeypatch, missing):
    from agent_py_agent.agent.conversation.business_access import BusinessError
    from agent_py_agent.agent.conversation.session_messaging import authorize_business_child
    from agent_py_agent.agent.subagents import authorization_gate

    services, _context, actor, _target, _space = dispatch_case
    services.host.subagents = object()
    def denied(*args, **kwargs):
        if missing:
            raise FileNotFoundError("private child path")
        raise authorization_gate.AuthorizationError("private foreign owner")
    monkeypatch.setattr(authorization_gate, "authorize_direct_child_operation", denied)
    with pytest.raises(BusinessError) as failure:
        authorize_business_child(services.host, "foreign-child", actor, "send_guidance")
    assert api._error_code(failure.value) == (("RESOURCE_NOT_FOUND", 404, "not_started") if missing else
        ("BUSINESS_ACCESS_DENIED", 403, "not_started"))
    assert "private foreign owner" not in str(failure.value.details)


@pytest.mark.parametrize("error_type", [PermissionError, KeyError, FileNotFoundError])
def test_post_enqueue_authorization_exception_remains_unknown(dispatch_case, monkeypatch, error_type):
    services, context, _actor, target, _space = dispatch_case
    def failed_wake(*args, **kwargs):
        raise error_type("private post-enqueue failure")
    monkeypatch.setattr(services.host.conversation_store.wakes, "raise_signal", failed_wake)
    status, result = api.execute_business({"request_id": "late-auth-failure",
        "action": "background_agents.messages.send",
        "params": {"agent_id": target.thread_id, "text": "already queued", "input_kind": "message"},
        "idempotency_key": "late-auth-failure"}, context, services)
    assert status == 503 and result["effect_outcome"] == "unknown"
    assert result["error"]["code"] == "OUTCOME_UNKNOWN"
    assert {row["key"]: row["value"] for row in result["error"]["details"]}["retry_same_request"] is False


def test_submit_wrong_controller_rejected_before_creation_then_correct_id_is_unique_on_wire(background_http):
    params, agent = create_background(background_http)
    server, _ = background_http
    services = next(item for item in server._business_services.values() if item.owner_id.endswith('/business-alice'))
    store = services.host.conversation_store
    status, read = background_call(background_http, 'controllers.get')
    assert status == 200 and not read['data']['available'], read
    expected = read['data']['agent_id']
    assert expected == read['data']['thread_id'] and store.threads.load(expected) is None
    status, refused = background_call(background_http, 'tasks.submit', {
        'space_id': params['space_id'], 'controller_agent_id': agent['thread_id'], 'instruction': '一次测试任务'},
        idempotency_key='wrong-controller')
    assert status == 409 and refused['effect_outcome'] == 'not_started', refused
    assert refused['error']['code'] == 'CONTROLLER_IDENTITY_CONFLICT'
    details = {row['key']: row['value'] for row in refused['error']['details']}
    assert details['field_path'] == '$.params.controller_agent_id'
    assert details['actual'] == agent['thread_id'] and details['expected'] == expected
    assert details['controller_created'] is False and details['task_created'] is False
    assert 'controllers.get' in details['next_action']
    assert store.threads.load(expected) is None
    assert store.session_tasks.list_page()['items'] == []
    request = {'space_id': params['space_id'], 'controller_agent_id': expected, 'instruction': '一次测试任务'}
    status, accepted = background_call(background_http, 'tasks.submit', request, idempotency_key='correct-controller')
    assert status == 200, accepted
    replay_status, replay = background_call(background_http, 'tasks.submit', request, idempotency_key='correct-controller')
    assert replay_status == 200 and replay['data']['task_id'] == accepted['data']['task_id'], replay
    tasks = store.session_tasks.list_page()['items']
    assert len(tasks) == 1 and tasks[0].target_thread_id == expected
    assert store.threads.require(expected).metadata['business_controller'] is True


def test_submit_controller_preparation_then_known_refusal_exposes_committed_controller(background_http, monkeypatch):
    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
    from agent_py_agent.agent.conversation.business_access import BusinessError

    params, _agent = create_background(background_http)
    def refuse(*args, **kwargs):
        raise BusinessError('SESSION_TASK_RATE_LIMIT')
    monkeypatch.setattr(BackgroundAgentService, '_dispatch', refuse)
    status, result = background_call(background_http, 'tasks.submit',
        {'space_id': params['space_id'], 'instruction': '不能派发'}, idempotency_key='post-prepare-refusal')
    assert status == 409 and result['effect_outcome'] == 'committed', result
    assert not result['error']['retryable']
    details = {row['key']: row['value'] for row in result['error']['details']}
    assert details['controller_created'] is True and details['task_created'] is False
    assert details['side_effect_state'] == 'controller_created_task_not_started'


def test_submit_controller_preparation_then_dispatch_unknown_never_claims_no_task(background_http, monkeypatch):
    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
    from agent_py_agent.agent.conversation.session_task_dispatch import (
        SessionTaskDispatchError,
        session_task_dispatch_error,
    )

    params, _agent = create_background(background_http)
    def unknown(*args, **kwargs):
        raise SessionTaskDispatchError(session_task_dispatch_error('原派发待核对',
            error_code='TOOL_EXECUTION_FAILED', effect_outcome='unknown'))
    monkeypatch.setattr(BackgroundAgentService, '_dispatch', unknown)
    status, result = background_call(background_http, 'tasks.submit',
        {'space_id': params['space_id'], 'instruction': '原账待核实'}, idempotency_key='post-prepare-unknown')
    assert status == 503 and result['effect_outcome'] == 'unknown', result
    assert result['error']['retryable'] is True  # 冻结协议仅表示503可恢复，重放权由下面显式字段拒绝。
    details = {row['key']: row['value'] for row in result['error']['details']}
    assert details['retry_same_request'] is False
    assert details['controller_created'] is True and details['task_dispatch_outcome'] == 'unknown'
    assert 'task_created' not in details and '禁止' in details['next_action']


def test_submit_controller_rechecks_identity_under_original_lock_and_denies_foreign_actor(dispatch_case, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
    from agent_py_agent.agent.conversation.business_access import BusinessError

    services, _context, actor, _target, space = dispatch_case
    services.host.business_spaces_catalog = services.spaces
    service = BackgroundAgentService(services.host)
    expected = service.get_controller(actor=actor)['agent_id']
    count = 0
    original = service.get_controller
    def drift(*, actor):
        nonlocal count
        count += 1
        value = original(actor=actor)
        return {**value, 'agent_id': 'changed-controller'} if count == 2 else value
    monkeypatch.setattr(service, 'get_controller', drift)
    with pytest.raises(BusinessError) as caught:
        service.submit({'space_id': space, 'controller_agent_id': expected, 'instruction': '一次'},
            actor=actor, idempotency_key='identity-drift')
    assert caught.value.code == 'CONTROLLER_IDENTITY_CONFLICT'
    assert caught.value.details['reason'] == 'CONTROLLER_IDENTITY_DRIFT'
    assert services.host.conversation_store.threads.load(expected) is None
    with pytest.raises(BusinessError) as caught:
        service.submit({'space_id': space, 'instruction': '不允许'}, actor=replace(actor, owner_id='local/foreign'),
            idempotency_key='foreign-submit')
    assert caught.value.code == 'BUSINESS_ACCESS_DENIED'
    assert expected not in str(caught.value.details) and 'expected' not in caught.value.details


@pytest.mark.parametrize('effect', ['not_started', 'committed', 'unknown'])
def test_business_error_explicit_effect_uses_frozen_http_envelope_without_opaque_masking(effect):
    from agent_py_agent.agent.conversation.business_access import BusinessError
    from agent_py_agent.agent.gateway_parts.business_api import _error_code

    assert _error_code(BusinessError('STATE_CONFLICT', effect_outcome=effect)) == ('STATE_CONFLICT', 409, effect)
    assert _error_code(BusinessError('STATE_CONFLICT')) == ('STATE_CONFLICT', 409, 'not_started')
    with pytest.raises(ValueError):
        BusinessError('STATE_CONFLICT', effect_outcome='succeeded')


def test_submit_real_post_creation_wake_failure_keeps_existing_task_unknown(background_http, monkeypatch):
    from agent_py_agent.agent.conversation import session_task_dispatch

    params, _agent = create_background(background_http)
    def failed_wake(*args, **kwargs):
        raise OSError('post-canonical wake failure')
    monkeypatch.setattr(session_task_dispatch, '_maybe_wake', failed_wake)
    status, result = background_call(background_http, 'tasks.submit',
        {'space_id': params['space_id'], 'instruction': '原task已创建'}, idempotency_key='actual-canonical-unknown')
    assert status == 503 and result['effect_outcome'] == 'unknown', result
    details = {row['key']: row['value'] for row in result['error']['details']}
    assert details['controller_created'] is True and 'task_created' not in details
    assert details['retry_same_request'] is False
    services = next(item for item in background_http[0]._business_services.values()
        if item.owner_id.endswith('/business-alice'))
    tasks = services.host.conversation_store.session_tasks.list_page()['items']
    assert len(tasks) == 1 and tasks[0].body_guidance_id


def test_submit_existing_controller_refusal_has_no_new_controller_mutation(background_http, monkeypatch):
    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
    from agent_py_agent.agent.conversation.business_access import BusinessError

    params, _agent = create_background(background_http)
    status, prepared = background_call(background_http, 'controllers.ensure', idempotency_key='existing-controller')
    assert status == 200 and prepared['data']['created'], prepared
    def refuse(*args, **kwargs):
        raise BusinessError('SESSION_TASK_RATE_LIMIT')
    monkeypatch.setattr(BackgroundAgentService, '_dispatch', refuse)
    status, result = background_call(background_http, 'tasks.submit',
        {'space_id': params['space_id'], 'instruction': '已有总控仍未派任务'}, idempotency_key='existing-refusal')
    assert status == 409 and result['effect_outcome'] == 'not_started', result
    assert not any(row['key'] == 'controller_created' and row['value'] is True
        for row in result['error']['details'])


@pytest.mark.parametrize('project', ['workflow-is-not-project', '../other-owner', '/private/other-owner'])
def test_submit_project_ref_is_readonly_preflight_before_controller_or_task(background_http, project):
    params, _agent = create_background(background_http)
    server, _ = background_http
    services = next(item for item in server._business_services.values() if item.owner_id.endswith('/business-alice'))
    from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports

    wire_business_host_ports(services.host)
    services.spaces.reference_resolver = services.host.business_space_reference_resolver
    services.host.business_spaces_catalog = services.spaces
    _, view = background_call(background_http, 'controllers.get')
    controller = view['data']['agent_id']
    assert services.host.conversation_store.threads.load(controller) is None
    status, result = background_call(background_http, 'tasks.submit', {
        'space_id': params['space_id'], 'instruction': '项目引用核验', 'project_ref': project},
        idempotency_key='invalid-project-preflight')
    assert status in (400, 404), result
    assert result['effect_outcome'] == 'not_started'
    details = {row['key']: row['value'] for row in result['error']['details']}
    assert details['field_path'] == '$.params.project_ref' and details['task_created'] is False
    assert '省略project_ref' in details['next_action']
    assert project not in str(result)
    assert services.host.conversation_store.threads.load(controller) is None
    assert not list(services.host.conversation_store.session_tasks._dedupe_path('unused').parent.glob('*.json'))


def test_submit_real_project_and_original_frozen_replay(background_http):
    params, _agent = create_background(background_http)
    server, _ = background_http
    services = next(item for item in server._business_services.values() if item.owner_id.endswith('/business-alice'))
    from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports

    wire_business_host_ports(services.host)
    services.spaces.reference_resolver = services.host.business_space_reference_resolver
    services.host.business_spaces_catalog = services.spaces
    project = services.owner_home / 'projects' / 'real-project'
    project.mkdir(parents=True)
    body = {'space_id': params['space_id'], 'instruction': '合法项目派活', 'project_ref': 'real-project'}
    status, first = background_call(background_http, 'tasks.submit', body, idempotency_key='real-project-submit')
    assert status == 200, first
    task = services.host.conversation_store.session_tasks.require(first['data']['task_id'])
    assert task.metadata['business_context']['project_ref'] == 'real-project'
    project.rmdir()
    status, replay = background_call(background_http, 'tasks.submit', body, idempotency_key='real-project-submit')
    assert status == 200 and replay['data']['task_id'] == task.task_id, replay


def test_project_permission_change_at_canonical_factory_preserves_partial_unknown(background_http, monkeypatch):
    from agent_py_agent.agent.conversation.business_access import BusinessError
    from agent_py_agent.agent.gateway_parts.business_host_ports import wire_business_host_ports

    params, _agent = create_background(background_http)
    server, _ = background_http
    services = next(item for item in server._business_services.values() if item.owner_id.endswith('/business-alice'))
    wire_business_host_ports(services.host)
    services.spaces.reference_resolver = services.host.business_space_reference_resolver
    (services.owner_home / 'projects' / 'real-project').mkdir(parents=True)
    original = services.spaces.reference
    calls = []
    def revoked(kind, ref, **kwargs):
        calls.append((kind, ref))
        if len(calls) == 3:
            raise BusinessError('BUSINESS_ACCESS_DENIED')
        return original(kind, ref, **kwargs)
    monkeypatch.setattr(services.spaces, 'reference', revoked)
    status, result = background_call(background_http, 'tasks.submit', {
        'space_id': params['space_id'], 'instruction': '执行前撤权', 'project_ref': 'real-project'},
        idempotency_key='project-revoke-at-factory')
    assert status == 503 and result['effect_outcome'] == 'unknown', result
    details = {row['key']: row['value'] for row in result['error']['details']}
    assert details['controller_created'] is True and 'task_created' not in details
    assert details['exception_type'] == 'BusinessError'
    assert details['cause_code'] == 'BUSINESS_ACCESS_DENIED' and details['stage'] == 'create_task'
    assert details['retry_same_request'] is False
    assert list(services.host.conversation_store.session_tasks._dedupe_path('unused').parent.glob('*.json'))


def test_workflow_descriptor_protocol_rejected_before_canonical_creation_on_wire(background_http):
    params, _agent = create_background(background_http)
    server, _ = background_http
    services = next(item for item in server._business_services.values() if item.owner_id.endswith('/business-alice'))
    status, result = background_call(background_http, 'workflows.create', {
        'definition': {'descriptor': {'id': 'bad-protocol', 'kind': 'workflow', 'name': '坏协议输入',
            'schema_version': '1', 'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
            'steps': []}, 'scope': {'scope_mode': 'owner_global', 'space_ids': []}},
        idempotency_key='bad-workflow-protocol')
    assert status == 400 and result['effect_outcome'] == 'not_started', result
    assert result['error']['code'] == 'INVALID_SCHEMA'
    details = {row['key']: row['value'] for row in result['error']['details']}
    assert details['field'] == 'definition.descriptor.schema_version' and details['expected'] == 'asset_descriptor.v1'
    assert not (services.owner_home / 'workflows' / 'bad-protocol').exists()
