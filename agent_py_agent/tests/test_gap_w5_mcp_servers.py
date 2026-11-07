"""MCP 配置、原参数事务及单服务目录的隔离回归；不启动任何外部服务。"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from itertools import count
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import gap158_w5_mcp_servers as mcp
from agent_py_agent.agent.gateway_parts.business_api import bind_business_context
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.settings import parameter_changes as changes
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.parameter_registry import STRUCTURED_PARAMETER_KEYS
from agent_py_agent.agent.tooling.mcp_client import MCPError, MCPServerConfig, MCPToolInfo
from agent_py_agent.agent.tooling.mcp_registration import build_proxy_tool


@pytest.fixture
def calls(tmp_path, monkeypatch):
    """发行真实 live context，测试替身只替换不应连接网络的注册表。"""
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('MY_AGENT_CONFIG', raising=False)
    owner = tmp_path / 'owner'
    owner.mkdir()
    path = tmp_path / 'deployment.yaml'
    path.write_text('agent_name: test\nbusiness_spaces_enabled: true\n', encoding='utf-8')
    paths = SimpleNamespace(owner_id='local/main', owner_home_dir=owner,
                            owner_provider='local', owner_kind='main', root=tmp_path)
    host = SimpleNamespace(home_paths=paths, config=AgentConfig(config_path=str(path), business_spaces_enabled=True),
                           tools=SimpleNamespace(tools={}, _mcp_clients=[], _mcp_prepare_lock=threading.Lock()))
    context = bind_business_context(host, authorize=lambda action, facts: True, subject_id='tester')
    services = SimpleNamespace(host=host, owner_id=paths.owner_id)

    sequence = count(1)

    def make(action, params=None, **kwargs):
        key = kwargs.pop('idempotency_key', f'test-operation-{next(sequence):04}')
        return V13Call(action, params or {}, 'request', paths.owner_id, 'tester', context,
                       services, owner, idempotency_key=key, **kwargs)
    return make, path, host


def _entry():
    return {'command': 'python3', 'args': ['service.py'], 'env': {'VISIBLE': 'fixture-value', 'TOKEN': 'fixture-secret'}}


def test_validator_uses_each_real_server_constructor(monkeypatch):
    assert 'mcp_servers' in STRUCTURED_PARAMETER_KEYS
    original = MCPServerConfig.from_mapping
    seen = []

    def validate(name, raw):
        seen.append(name)
        return original(name, raw)
    monkeypatch.setattr(MCPServerConfig, 'from_mapping', validate)
    value = {'one': _entry(), 'two': {'command': 'node', 'args': ['service.js']}}
    expected, _ = changes._render_structured('mcp_servers', value)
    assert expected == value and seen == ['one', 'two']


@pytest.mark.parametrize('value', [[], {'bad': None}, {'bad': {}}, {'Bad': {'command': 'x'}},
    {'ok': {'command': 'x', 'tool_effects': {'x': 'invalid'}}},
    {'ok': {'command': 'x', 'catalog_category': 'bad category'}},
    '{"same":{"command":"x"},"same":{"command":"y"}}'])
def test_validator_rejects_bad_entry_instead_of_skipping(value):
    with pytest.raises(changes.ParameterChangeError) as error:
        changes._render_structured('mcp_servers', value)
    assert error.value.code == 'PARAMETER_INVALID'


def test_mcp_writer_does_not_expand_cli_or_user_settings_permissions(calls):
    _, path, _ = calls
    kwargs = {'paths': changes.WritePaths(path), 'origin': changes.ChangeOrigin('test')}
    original = path.read_bytes()
    for scope in (changes.user_settings_write_scope, changes.cli_deployment_write_scope):
        with scope():
            report = changes.set_parameter('mcp_servers', {'one': _entry()}, **kwargs)
        assert report['ok'] is False
    assert not changes.set_parameter('mcp_servers', {'one': _entry()}, **kwargs)['ok']
    assert path.read_bytes() == original and not changes.ledger_path(path).exists()


def test_create_delete_use_real_transaction_cas_and_redacted_ledger(calls):
    make, path, _ = calls
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    saved = mcp.create(make('mcp_servers.create', {'name': 'one', 'entry': _entry(),
                                               'expected_config_revision': before}))
    assert saved['server']['name'] == 'one' and saved['config_revision'] != before
    assert load_config(path).mcp_servers['one']['env']['TOKEN'] == 'fixture-secret'
    encoded = json.dumps(saved)
    ledger = changes.ledger_path(path).read_text()
    assert 'fixture-secret' not in encoded + ledger and 'fixture-value' not in encoded + ledger
    listed = mcp.list_servers(make('mcp_servers.list'))
    assert listed['config_revision'] == saved['config_revision']
    assert listed['items'][0]['tool_count'] == 0
    with pytest.raises(HostAPIError) as conflict:
        mcp.delete(make('mcp_servers.delete', {'name': 'one', 'expected_config_revision': before}))
    assert conflict.value.code == 'REVISION_CONFLICT'
    deleted = mcp.delete(make('mcp_servers.delete', {'name': 'one',
        'expected_config_revision': saved['config_revision']}))
    assert deleted['deleted'] is True and load_config(path).mcp_servers == {}
    assert mcp.delete(make('mcp_servers.delete', {'name': 'one'}))['deleted'] is False
    assert [row['key'] for row in changes.parameter_history(user_path=path)] == ['mcp_servers', 'mcp_servers']


def test_duplicate_and_invalid_create_do_not_modify_config(calls):
    make, path, _ = calls
    mcp.create(make('mcp_servers.create', {'name': 'one', 'entry': _entry()}))
    before = path.read_bytes(), changes.ledger_path(path).read_bytes()
    for params, code in [({'name': 'one', 'entry': _entry()}, 'STATE_CONFLICT'),
                          ({'name': 'two', 'entry': {}}, 'INVALID_SCHEMA')]:
        with pytest.raises(HostAPIError) as error:
            mcp.create(make('mcp_servers.create', params))
        assert error.value.code == code
        assert before == (path.read_bytes(), changes.ledger_path(path).read_bytes())


def test_mcp_admin_and_owner_identity_are_rechecked(calls):
    make, path, host = calls
    host.business_native_admin = lambda: False
    with pytest.raises(HostAPIError) as denied:
        mcp.create(make('mcp_servers.create', {'name': 'one', 'entry': _entry()}))
    assert denied.value.code == 'BUSINESS_ACCESS_DENIED'
    with pytest.raises(HostAPIError) as foreign:
        mcp.list_servers(replace(make('mcp_servers.list'), owner_id='local/foreign'))
    assert foreign.value.code == 'RESOURCE_NOT_FOUND'
    assert not changes.ledger_path(path).exists()


def _client(name, running=True):
    transport = object()
    return SimpleNamespace(config=MCPServerConfig(name, 'fixture'), publication=None,
                           is_running=lambda: running, connection=lambda: transport)


def test_tools_are_paginated_from_existing_named_proxies_and_refresh_is_targeted(calls, monkeypatch):
    make, _, host = calls
    for name in ('one', 'two'):
        mcp.create(make('mcp_servers.create', {'name': name, 'entry': _entry()}))
    one, two = _client('one'), _client('two')
    host.tools._mcp_clients = [one, two]
    for client in (one, two):
        for name in ('alpha', 'beta'):
            proxy = build_proxy_tool(client, client.config.name, MCPToolInfo(name, name, {'type': 'object'}),
                                     effect='read_only', transport=client.connection())
            host.tools.tools[proxy.model_spec.name] = proxy
    first = mcp.tools(make('mcp_servers.tools', {'name': 'one', 'limit': 1}))
    assert first['connected'] is True and first['coverage'] == 'partial'
    second = mcp.tools(make('mcp_servers.tools', {'name': 'one', 'limit': 1, 'cursor': first['next_cursor']}))
    assert first['items'][0]['name'] != second['items'][0]['name']
    assert all('two' not in row['model_name'] for page in (first, second) for row in page['items'])
    with pytest.raises(Exception) as bad:
        mcp.tools(make('mcp_servers.tools', {'name': 'two', 'cursor': first['next_cursor']}))
    assert bad.value.code == 'CURSOR_EXPIRED'
    seen = []

    def refresh(registry, client, *, transport):
        seen.append((registry, client, transport))
        return 2
    monkeypatch.setattr(mcp, 'refresh_registered_mcp_client', refresh)
    result = mcp.refresh(make('mcp_servers.refresh', {'name': 'one'}))
    assert result == {'name': 'one', 'connected': True, 'tool_count': 2}
    assert seen == [(host.tools, one, one.connection())]


def test_refresh_failure_reports_stable_code_without_reconnecting(calls, monkeypatch):
    make, _, host = calls
    mcp.create(make('mcp_servers.create', {'name': 'one', 'entry': _entry()}))
    host.tools._mcp_clients = [_client('one', running=False)]

    def unavailable(*args, **kwargs):
        raise MCPError('fixture', code='MCP_CONNECTION_CLOSED')
    monkeypatch.setattr(mcp, 'refresh_registered_mcp_client', unavailable)
    result = mcp.refresh(make('mcp_servers.refresh', {'name': 'one'}))
    assert result['connected'] is False and result['error_code'] == 'MCP_CONNECTION_CLOSED'


def test_batch_parameter_transaction_is_atomic_and_each_key_authorized(calls, monkeypatch):
    _, path, _ = calls
    before = path.read_bytes()
    report = changes.set_parameters({'tool_catalog_categories': ['web'], 'tool_catalog_deferred_categories': [1]},
        paths=changes.WritePaths(path), origin=changes.ChangeOrigin('test'))
    assert not report['ok'] and path.read_bytes() == before and not changes.ledger_path(path).exists()
    report = changes.set_parameters({'tool_catalog_categories': ['web'], 'tool_catalog_deferred_categories': []},
        paths=changes.WritePaths(path, expected_revision=hashlib.sha256(before).hexdigest()),
        origin=changes.ChangeOrigin('test'))
    assert report['ok'] and len(report['changes']) == 2
    assert load_config(path).tool_catalog_categories == ['web']
    assert load_config(path).tool_catalog_deferred_categories == []
    snapshot = path.read_bytes(), changes.ledger_path(path).read_bytes()
    report = changes.set_parameters({'tool_catalog_categories': [], 'mcp_servers': {'bad': _entry()}},
        paths=changes.WritePaths(path), origin=changes.ChangeOrigin('test'))
    assert not report['ok'] and snapshot == (path.read_bytes(), changes.ledger_path(path).read_bytes())


@pytest.fixture
def w5_registry(monkeypatch):
    """用原加载器装配当前切片；W0 尚未合入时只在测试中补静态模块名，不改生产注册表。"""
    from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
    name = 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w5'
    names = v13.V13_SLICE_MODULES
    tables = v13._load_registry(names if name in names else (*names, name))
    for key, value in zip(('_ACTIONS', 'ALIASES', 'V13_DEFS', 'JOB_KINDS', 'COLLECTIONS'), tables):
        monkeypatch.setattr(v13, key, value)
    return v13


def test_w5_static_actions_have_closed_resolvable_schemas(w5_registry):
    from agent_py_agent.agent.gateway_parts import business_api_v13_gap158_w5 as slice_module
    expected = {*mcp.HANDLERS,
        'skills_pkg.upload_zip', 'skills_pkg.missing', 'skills_pkg.usage', 'skills_pkg.dirs.create',
        'skills_pkg.files.list', 'skills_pkg.files.read', 'skills_pkg.files.write', 'skills_pkg.files.delete',
        'custom_tools.list', 'custom_tools.configure', 'custom_tools.reset', 'custom_tools.create',
        'custom_tools.update', 'custom_tools.delete', 'custom_tools.test', 'custom_tools.python_detect'}
    assert {action.name for action in slice_module.V13_ACTIONS} == expected
    assert w5_registry.COLLECTIONS['custom_tools'] == 'owner'
    for action in slice_module.V13_ACTIONS:
        spec = w5_registry.v13_action_spec(action.name)
        assert spec.params_schema['additionalProperties'] is False
        assert spec.result_schema['additionalProperties'] is False
        assert spec.requires_idempotency == (action.effect != 'read')
        assert bool(action.ready) == (action.name in {'custom_tools.create', 'custom_tools.update',
                                                     'custom_tools.delete', 'custom_tools.test'})


def test_dispatcher_checks_schema_admin_gate_and_post_commit_result(calls, w5_registry):
    from agent_py_agent.agent.gateway_parts.business_api import BusinessServices, execute_business
    make, _, host = calls
    services = BusinessServices(host)
    context = make('mcp_servers.list').context
    request = {'action': 'mcp_servers.create', 'request_id': 'wire-create',
               'params': {'name': 'one', 'entry': _entry()}, 'idempotency_key': 'wire-operation-0001'}
    status, result = execute_business(request, context, services)
    assert status == 200 and result['state'] == 'completed' and result['effect_outcome'] == 'committed'
    assert result['data']['server']['name'] == 'one'
    status, result = execute_business({'action': 'mcp_servers.list', 'request_id': 'wire-list', 'params': {}},
                                     context, services)
    assert status == 200 and len(result['data']['items']) == 1
    bad = {**request, 'params': {**request['params'], 'owner_id': 'foreign'}}
    status, result = execute_business(bad, context, services)
    assert status == 400 and result['error']['code'] == 'INVALID_SCHEMA'
    host.business_native_admin = lambda: False
    status, result = execute_business({**request, 'params': {'name': 'two', 'entry': _entry()}}, context, services)
    assert status == 403 and result['error']['code'] == 'BUSINESS_ACCESS_DENIED'
    status, result = execute_business({'action': 'custom_tools.create', 'request_id': 'wire-switch',
        'params': {'name': 'one', 'description': 'test', 'language': 'python', 'entry_script': 'print(1)',
                   'input_schema': {'type': 'object'}}, 'idempotency_key': 'wire-operation-0002'}, context, services)
    assert status == 503 and result['error']['code'] == 'HOST_NOT_READY'


@pytest.fixture
def native_w5(w5_registry, tmp_path, monkeypatch):
    """沿原 Gateway 和真实 echo Agent 启停，只注入隔离配置，不替换 HTTP/认证/领域处理器。"""
    from agent_py_agent.tests import test_v13_w0_harness as harness
    yield from harness.v13_native.__wrapped__(tmp_path, monkeypatch)


def test_real_http_skill_install_file_cas_owner_isolation_and_switch(native_w5):
    import base64
    import io
    import zipfile

    from agent_py_agent.tests.test_v13_w0_harness import v13_call
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('SKILL.md', '---\nname: sample\ndescription: 演示方法\n---\n\n安全正文。\n')
    archive_b64 = base64.b64encode(data.getvalue()).decode()
    user = 'business-alice'
    status, body = v13_call(native_w5, 'skills_pkg.upload_zip', {'name': 'sample', 'archive_b64': archive_b64},
                           user=user, idempotency_key='http-w5-upload-0001')
    assert status == 200, body
    assert body['data']['skill']['name'] == 'sample' and body['effect_outcome'] == 'committed'
    status, body = v13_call(native_w5, 'skills_pkg.dirs.create', {'name': 'sample', 'dir': 'references'},
                           user=user, idempotency_key='http-w5-dir-0000001')
    assert status == 200 and body['data']['created'] is True, body
    params = {'name': 'sample', 'path': 'references/note.txt', 'content_b64': base64.b64encode(b'hello').decode(),
              'expected_sha256': 'absent'}
    status, body = v13_call(native_w5, 'skills_pkg.files.write', params, user=user,
                           idempotency_key='http-w5-write-00001')
    assert status == 200, body
    digest = body['data']['sha256']
    status, body = v13_call(native_w5, 'skills_pkg.files.read', {'name': 'sample', 'path': 'references/note.txt',
                           'max_bytes': 2}, user=user)
    assert status == 200 and body['data']['content_b64'] == base64.b64encode(b'he').decode(), body
    assert body['data']['sha256'] == digest and body['data']['truncated'] is True
    status, body = v13_call(native_w5, 'skills_pkg.files.read', {'name': 'sample', 'path': 'references/note.txt'},
                           user='business-bob')
    assert status == 404 and body['error']['code'] == 'RESOURCE_NOT_FOUND', body
    status, body = v13_call(native_w5, 'skills_pkg.files.write', params, user=user,
                           idempotency_key='http-w5-conflict-001')
    assert status == 409 and body['error']['code'] == 'REVISION_CONFLICT', body
    status, body = v13_call(native_w5, 'custom_tools.create', {'name': 'sample', 'description': 'test',
        'language': 'python', 'entry_script': 'print(1)', 'input_schema': {'type': 'object'}}, user=user,
        idempotency_key='http-w5-disabled-001')
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY', body
    status, body = v13_call(native_w5, 'mcp_servers.delete', {'name': 'sample'}, user=user,
                           idempotency_key='http-w5-no-admin-001')
    assert status == 403 and body['error']['code'] == 'BUSINESS_ACCESS_DENIED', body


def test_create_replay_and_parameter_conflict_are_durable(calls):
    make, path, _ = calls
    call = make('mcp_servers.create', {'name': 'one', 'entry': _entry()}, idempotency_key='replay-create-0001')
    saved = mcp.create(call)
    before = path.read_bytes(), changes.ledger_path(path).read_bytes()
    assert mcp.create(call) == saved
    assert before == (path.read_bytes(), changes.ledger_path(path).read_bytes())
    with pytest.raises(HostAPIError) as conflict:
        mcp.create(replace(call, params={'name': 'one', 'entry': {'command': 'changed'}}))
    assert conflict.value.code == 'IDEMPOTENCY_CONFLICT'


def test_delete_replay_cannot_remove_a_recreated_server(calls):
    make, path, _ = calls
    mcp.create(make('mcp_servers.create', {'name': 'one', 'entry': _entry()}))
    call = make('mcp_servers.delete', {'name': 'one'}, idempotency_key='replay-delete-0001')
    deleted = mcp.delete(call)
    mcp.create(make('mcp_servers.create', {'name': 'one', 'entry': {'command': 'new-generation'}}))
    before = path.read_bytes(), changes.ledger_path(path).read_bytes()
    assert mcp.delete(call) == deleted
    assert load_config(path).mcp_servers['one']['command'] == 'new-generation'
    assert before == (path.read_bytes(), changes.ledger_path(path).read_bytes())


def test_uncertain_config_commit_is_never_executed_twice(calls, monkeypatch):
    make, path, _ = calls
    call = make('mcp_servers.create', {'name': 'one', 'entry': _entry()}, idempotency_key='unknown-create-001')
    original = changes.set_parameter
    invocations = []

    def interrupted(*args, **kwargs):
        invocations.append(True)
        original(*args, **kwargs)
        raise OSError('fixture after commit')
    monkeypatch.setattr(changes, 'set_parameter', interrupted)
    for _ in range(2):
        with pytest.raises(HostAPIError) as error:
            mcp.create(call)
        assert (error.value.code, error.value.outcome) == ('OUTCOME_UNKNOWN', 'unknown')
    assert invocations == [True]
    assert load_config(path).mcp_servers['one']['command'] == 'python3'


def test_refresh_replays_without_calling_the_server_again(calls, monkeypatch):
    make, _, host = calls
    mcp.create(make('mcp_servers.create', {'name': 'one', 'entry': _entry()}))
    host.tools._mcp_clients = [_client('one')]
    invocations = []
    monkeypatch.setattr(mcp, 'refresh_registered_mcp_client', lambda *args, **kwargs: invocations.append(True) or 0)
    call = make('mcp_servers.refresh', {'name': 'one'}, idempotency_key='replay-refresh-001')
    result = mcp.refresh(call)
    assert mcp.refresh(call) == result and invocations == [True]


def test_named_deployment_server_cannot_adopt_a_plugin_client(calls, monkeypatch):
    from agent_py_agent.agent.plugin_runtime import PluginMCPClient
    make, _, host = calls
    mcp.create(make('mcp_servers.create', {'name': 'plugin_demo', 'entry': _entry()}))

    class PluginStub(PluginMCPClient):
        def __init__(self):
            self.config = MCPServerConfig('plugin_demo', 'fixture')
            self.publication = None
        def is_running(self):
            return True
        def connection(self):
            return object()

    plugin = PluginStub()
    host.tools._mcp_clients = [plugin]
    proxy = build_proxy_tool(plugin, 'plugin_demo', MCPToolInfo('alpha', 'plugin tool', {'type': 'object'}),
                             effect='read_only', transport=object())
    host.tools.tools[proxy.model_spec.name] = proxy
    seen = []
    monkeypatch.setattr(mcp, 'refresh_registered_mcp_client', lambda *args, **kwargs: seen.append(True) or 1)
    listed = mcp.list_servers(make('mcp_servers.list'))['items'][0]
    assert listed['connected'] is False and listed['tool_count'] == 0
    assert mcp.tools(make('mcp_servers.tools', {'name': 'plugin_demo'}))['items'] == []
    result = mcp.refresh(make('mcp_servers.refresh', {'name': 'plugin_demo'}))
    assert result == {'name': 'plugin_demo', 'connected': False, 'tool_count': 0}
    assert not seen and host.tools.tools[proxy.model_spec.name] is proxy


def test_refresh_serializes_on_shared_catalog_owner(calls, monkeypatch):
    make, _, host = calls
    for name in ('one', 'two'):
        mcp.create(make('mcp_servers.create', {'name': name, 'entry': _entry()}))
    owner = SimpleNamespace(tools={'one': 'old', 'two': 'old'}, _mcp_clients=[_client('one'), _client('two')])
    first_entered, release_first, second_waiting = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == 'refresh-two':
                second_waiting.set()
            lock.acquire()
        def __exit__(self, *args):
            lock.release()

    owner._mcp_prepare_lock = ObservedLock()
    host.tools._mcp_catalog_owner = owner
    host.tools._mcp_clients = owner._mcp_clients
    entered, errors = [], []

    def refresh(registry, client, *, transport):
        assert registry is owner and lock.locked()
        previous = dict(registry.tools)
        entered.append(client.config.name)
        if client.config.name == 'one':
            first_entered.set()
            assert release_first.wait(5)
        previous[client.config.name] = 'new'
        registry.tools = previous
        return 0

    def invoke(name):
        try:
            mcp.refresh(make('mcp_servers.refresh', {'name': name}))
        except BaseException as error:
            errors.append(error)
    monkeypatch.setattr(mcp, 'refresh_registered_mcp_client', refresh)
    # 真实投影 helper 只处理 MCP 代理，此处目录保留两项并不需要复制伪造工具。
    one = threading.Thread(target=invoke, args=('one',), name='refresh-one')
    two = threading.Thread(target=invoke, args=('two',), name='refresh-two')
    one.start()
    try:
        assert first_entered.wait(5)
        two.start()
        assert second_waiting.wait(5)
        assert entered == ['one']
    finally:
        release_first.set()
        one.join(10)
        if two.ident is not None:
            two.join(10)
    assert not one.is_alive() and not two.is_alive() and not errors, errors
    assert entered == ['one', 'two'] and owner.tools == {'one': 'new', 'two': 'new'}


def test_real_http_python_detection_and_disabled_actions_are_not_advertised(native_w5):
    import sys
    from pathlib import Path

    from agent_py_agent.tests.test_v13_w0_harness import v13_call

    status, body = v13_call(native_w5, 'custom_tools.python_detect', {}, user='business-alice')
    assert status == 200, body
    interpreters = body['data']['interpreters']
    assert any(row['path'] == str(Path(sys.executable).resolve()) for row in interpreters), body
    assert all(type(row['free_threaded']) is bool and row['version'] for row in interpreters)
    status, body = v13_call(native_w5, 'system.capabilities.extensions', {}, user='business-alice')
    assert status == 200, body
    supported = set(body['data']['supported_actions'])
    assert {'skills_pkg.upload_zip', 'custom_tools.python_detect'} <= supported
    assert not ({'custom_tools.create', 'custom_tools.update', 'custom_tools.delete', 'custom_tools.test',
                 'mcp_servers.create', 'mcp_servers.delete', 'mcp_servers.refresh'} & supported)
