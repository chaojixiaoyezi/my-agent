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
    assert {key: get[1]["data"]["space"][key] for key in data} == data
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


def test_owner_maintenance_uses_real_w3_index_and_preserves_unknown_references(host):
    from datetime import datetime, timezone

    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_workflow_maintenance
    from agent_py_agent.tests.test_business_v12_f4_workflows import definition
    services = api.BusinessServices(host)
    services.workflows.clock = lambda: 100.0
    context = api.bind_business_context(host, authorize=lambda action, facts: True)
    actor = services.actor(context)
    services.workflows.execute('workflows.create', {'definition': definition(),
        'scope': {'scope_mode': 'unassigned', 'space_ids': []}}, actor=actor, idempotency_key='seed-orphan')
    now = datetime.fromtimestamp(40 * 86400, timezone.utc)
    result = run_owner_workflow_maintenance(host.home_paths, {}, now=now, safe_to_delete=True)
    assert result['executed'] is True
    assert result['counts'] == {'recycled': 0, 'skipped': 0, 'failed': 0, 'unknown': 1}
    assert run_owner_workflow_maintenance(host.home_paths, {}, now=now, safe_to_delete=True) == result
    current = services.workflows.execute('workflows.get', {'workflow_ref': 'daily'}, actor=actor)
    assert current['workflow']['lifecycle'] == 'active'


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
    assert services.observation.runtime.read_only and services.observation.runtime.existing_only


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
