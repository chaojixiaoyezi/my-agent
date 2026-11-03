"""#101：原生模型目录、真实W0持久化生命周期和HTTP入口回归，不触碰真实凭据或网络。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import v13_w10_credentials as credentials
from agent_py_agent.agent.business_spaces import v13_w10_identity as identity
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.tests.test_v13_w0_harness import v13_call

pytest_plugins = ('agent_py_agent.tests.test_v13_w0_harness',)


# LLM: 所有测试的原生路径只在临时HOME内，不读用户配置。
# 函数用途: 为原生只读测试配置隔离目录。
@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path))
    paths = home_paths(tmp_path / 'native')
    return SimpleNamespace(context=SimpleNamespace(host=SimpleNamespace(home_paths=paths)), params={})


# LLM: 这里只保存合成凭据，模型目录仍由产品原生操作器真实落盘。
# 函数用途: 配置一个测试服务商。
def save_native(call, *, provider='synthetic', endpoint='http://127.0.0.1:9999/v1'):
    credentials._settings(call).save_provider({'provider_id': provider, 'display_name': '测试服务商',
        'api_base': endpoint, 'api_key': 'synthetic-credential-not-real', 'models': [
            {'model_name': 'test-model', 'model_backend': 'openai_compatible'}]})


# LLM: 字面地址分类与出网状态必须独立，无配置或未知DNS不构成隔离证明。
# 函数用途: 核对真实目录读取的端点局部性。
def test_airgap_status_from_endpoint_locality(native):
    empty = credentials.airgap_status(native)
    assert empty['airgapped'] is None and empty['outbound_network'] == 'unknown'
    save_native(native)
    result = credentials.airgap_status(native)
    assert result['airgapped'] is True and result['model_endpoints'][0]['locality'] == 'loopback'
    save_native(native, endpoint='http://10.2.3.4/v1')
    assert credentials.airgap_status(native)['model_endpoints'][0]['locality'] == 'private'
    save_native(native, endpoint='https://8.8.8.8/v1')
    assert credentials.airgap_status(native)['airgapped'] is False
    save_native(native, endpoint='https://model.example.invalid/v1')
    assert credentials.airgap_status(native)['airgapped'] is None
    native.context.host.outbound_network = 'blocked'
    assert credentials.airgap_status(native)['outbound_network'] == 'blocked'


# LLM: 空参数示例必须真实读取原生目录，并且不公开secret。
# 函数用途: 验证模型状态与只读凭据投影。
def test_llm_status_example_empty_params(native):
    assert credentials.llm_status(native)['profiles'] == 0
    save_native(native)
    data = credentials.llm_status(native)
    assert data['profiles'] == 1 and data['selected_profile_id'] is None
    assert data['providers'][0]['models'] == ['test-model']
    assert data['providers'][0]['has_secret'] is True
    assert 'synthetic-credential-not-real' not in json.dumps(data)
    assert credentials.credentials_get(native)['providers'] == data['providers']
    native.params = {'provider_id': 'missing'}
    with pytest.raises(HostAPIError) as exc:
        credentials.credentials_get(native)
    assert exc.value.code == 'RESOURCE_NOT_FOUND'


# LLM: 直接测试产品验证器，断言固定结构化状态，不依赖异常文案。
# 函数用途: 拒绝探测器的非法字段及秘密回显。
@pytest.mark.parametrize('result', [{'ok': 'yes'}, {'ok': True, 'latency_ms': float('nan')},
    {'ok': False, 'latency_ms': -1}])
def test_probe_result_rejects_malformed_payload(result):
    with pytest.raises(HostAPIError) as exc:
        credentials._probe_result(result, 'synthetic')
    assert exc.value.code == 'HOST_NOT_READY'


# LLM: provider不能将任意错误文案或秘密塞进公开错误码。
# 函数用途: 核对探测错误码脱敏。
def test_probe_result_secret_redaction():
    result = credentials._probe_result({'ok': False, 'latency_ms': None,
        'error_code': 'SYNTHETIC_SECRET'}, 'SYNTHETIC_SECRET')
    assert result == {'ok': False, 'latency_ms': None, 'error_code': 'PROBE_FAILED'}


# LLM: 使用真实W0和issued业务context，不以替身存储冒充持久化验收。
# 函数用途: 构造可注入时钟、原生owner和live授权的领域测试环境。
@pytest.fixture
def env(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.business_api import (
        BusinessServices,
        bind_business_context,
    )
    from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
    from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path))
    root = tmp_path / 'isolated'
    lifecycle = LocalUserLifecycle(LocalUserStore(root))
    for user in ('alice', 'bob'):
        lifecycle.create(user, idempotency_key='credentials-test-user-' + user)
    clock, allowed, hosts = [1800000000.0], [True], {}
    for user in ('main', 'alice', 'bob'):
        owner = resolve_owner_home(root, OwnerIdentity.local_main() if user == 'main' else OwnerIdentity.local_user(user))
        paths = home_paths_with_owner(home_paths(root), owner)
        hosts[user] = SimpleNamespace(home_paths=paths, config=AgentConfig(business_spaces_enabled=True), business_v13_providers={})

    # LLM: 每次调用独立issued context；live状态可在读后、写前改变。
    # 函数用途: 生成标准F10调用。
    def call(action, params=None, *, user='main', key=None, revision=None):
        host = hosts[user]
        context = bind_business_context(host, authorize=lambda action, facts: allowed[0], subject_id=host.home_paths.owner_id)
        return V13Call(action=action, params=params or {}, request_id=uuid4().hex,
            owner_id=host.home_paths.owner_id, subject_id=host.home_paths.owner_id,
            context=context, services=BusinessServices(host), owner_home=host.home_paths.owner_home_dir,
            idempotency_key=key or uuid4().hex, expected_revision=revision, clock=lambda: clock[0])

    return SimpleNamespace(call=call, root=root, hosts=hosts, clock=clock, allowed=allowed)


# LLM: 生命周期覆盖首次秘密、重放、CAS、跨owner和撤销，不只测对象存在。
# 函数用途: 验证API key的真实持久化生命周期。
def test_api_key_lifecycle_secret_once_and_verify(env):
    create = env.call('api_keys.create', {'name': '测试key', 'scopes': ['api_keys.list'], 'expires_in_days': 1}, user='alice')
    first = credentials.api_keys_create(create)
    secret, row = first['secret'], first['key']
    assert secret.startswith('mak_') and len(secret) > 40
    assert credentials.api_keys_create(create)['secret'] == '<redacted>'
    stored = identity.one(create, 'api_keys', row['key_id'])
    assert stored['secret_sha256'] == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in json.dumps(stored)
    directory = create.owner_home / 'data' / 'business_v13'
    assert all(secret not in path.read_text() for path in directory.glob('*.json'))
    verify = env.call('api_keys.verify', {'secret': secret}, user='alice')
    assert credentials.api_keys_verify(verify) == {'valid': True, 'key_id': row['key_id'], 'scopes': ['api_keys.list']}
    assert not credentials.api_keys_verify(env.call('api_keys.verify', {'secret': secret}, user='bob'))['valid']
    assert not credentials.api_keys_verify(env.call('api_keys.verify', {'secret': 'wrong'}, user='alice'))['valid']
    assert credentials.api_keys_list(env.call('api_keys.list', user='bob')) == {'items': []}
    with pytest.raises(HostAPIError) as exc:
        credentials.api_keys_update(env.call('api_keys.update', {'key_id': row['key_id'], 'name': 'other'}, user='bob', revision=1))
    assert exc.value.code == 'RESOURCE_NOT_FOUND'
    current = identity.one(create, 'api_keys', row['key_id'])
    with pytest.raises(HostAPIError) as exc:
        credentials.api_keys_update(env.call('api_keys.update', {'key_id': row['key_id'], 'name': 'stale'}, user='alice', revision=0))
    assert exc.value.code == 'REVISION_CONFLICT'
    updated = credentials.api_keys_update(env.call('api_keys.update', {'key_id': row['key_id'],
        'name': '改名', 'scopes': []}, user='alice', revision=current['revision']))['key']
    assert updated['name'] == '改名' and updated['scopes'] == []
    credentials.api_keys_revoke(env.call('api_keys.revoke', {'key_id': row['key_id']}, user='alice'))
    assert not credentials.api_keys_verify(verify)['valid']
    assert credentials.api_keys_list(env.call('api_keys.list', user='alice')) == {'items': []}
    assert len(credentials.api_keys_list(env.call('api_keys.list', {'include_revoked': True}, user='alice'))['items']) == 1


# LLM: secret重放绝不重新生成，幂等键更换输入必须冲突。
# 函数用途: 核对scope目录、幂等冲突和精确到期边界。
def test_key_scope_idempotency_and_expiry(env):
    with pytest.raises(BusinessError) as exc:
        credentials.api_keys_create(env.call('api_keys.create', {'name': 'bad', 'scopes': ['no.such.permission']}))
    assert exc.value.code == 'INVALID_SCHEMA' and exc.value.details['path'] == 'params.scopes'
    call = env.call('api_keys.create', {'name': 'expiry', 'expires_in_days': 1})
    first = credentials.api_keys_create(call)
    with pytest.raises(BusinessError) as exc:
        credentials.api_keys_create(replace(call, params={'name': 'different'}))
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
    env.clock[0] += 86400
    assert not credentials.api_keys_verify(env.call('api_keys.verify', {'secret': first['secret']}))['valid']


# LLM: 频繁read验证不应每次改文件；live撤权在写之前必须挡住副作用。
# 函数用途: 核对每分钟使用时间节流与live检查。
def test_last_used_bounded_and_live_check(env, monkeypatch):
    create = env.call('api_keys.create', {'name': 'bounded'})
    data = credentials.api_keys_create(create)
    verify = env.call('api_keys.verify', {'secret': data['secret']})
    credentials.api_keys_verify(verify)
    path = create.owner_home / 'data' / 'business_v13' / 'api_keys.json'
    first = path.read_bytes()
    env.clock[0] += 59
    credentials.api_keys_verify(verify)
    assert path.read_bytes() == first
    env.clock[0] += 1
    credentials.api_keys_verify(verify)
    assert path.read_bytes() != first
    env.clock[0] += 60
    original = identity.rows

    # LLM: 确定性交错在读完后撤销live授权，不替换持久化或业务判断。
    # 函数用途: 模拟授权在读写之间失效。
    def revoke_after_read(call, collection, scope='owner'):
        result = original(call, collection, scope)
        env.allowed[0] = False
        return result

    monkeypatch.setattr(identity, 'rows', revoke_after_read)
    before = path.read_bytes()
    with pytest.raises(HostAPIError) as exc:
        credentials.api_keys_verify(verify)
    assert exc.value.code == 'CAPABILITY_DENIED' and path.read_bytes() == before


# LLM: 轮转与撤销必须即时生效，main集合管理仍依赖原生admin。
# 函数用途: 验证SCIM签发、CAS轮转、撤销和公开投影。
def test_scim_token_create_rotate_revoke_verify(env):
    call = env.call('scim_tokens.create', {'name': '同步', 'expires_in_days': 1})
    data = credentials.scim_create(call)
    assert credentials.scim_create(call)['secret'] == '<redacted>'
    assert credentials.scim_verify(env.call('scim_tokens.verify', {'secret': data['secret']}, user='alice'))['valid']
    current = identity.one(call, 'scim_tokens', data['token']['token_id'], 'main')
    with pytest.raises(HostAPIError) as exc:
        credentials.scim_rotate(env.call('scim_tokens.rotate', {'token_id': current['token_id']}, revision=0))
    assert exc.value.code == 'REVISION_CONFLICT'
    rotate = env.call('scim_tokens.rotate', {'token_id': current['token_id']}, revision=current['revision'])
    next_token = credentials.scim_rotate(rotate)
    assert next_token['token']['rotated_from'] == current['token_id']
    assert credentials.scim_rotate(rotate)['secret'] == '<redacted>'
    assert not credentials.scim_verify(env.call('scim_tokens.verify', {'secret': data['secret']}))['valid']
    assert credentials.scim_verify(env.call('scim_tokens.verify', {'secret': next_token['secret']}))['valid']
    credentials.scim_revoke(env.call('scim_tokens.revoke', {'token_id': next_token['token']['token_id']}))
    assert not credentials.scim_verify(env.call('scim_tokens.verify', {'secret': next_token['secret']}))['valid']
    assert all('secret_sha256' not in row for row in credentials.scim_list(env.call('scim_tokens.list'))['items'])
    for action, handler, params in [('scim_tokens.list', credentials.scim_list, {}),
            ('scim_tokens.create', credentials.scim_create, {'name': 'no'}),
            ('scim_tokens.rotate', credentials.scim_rotate, {'token_id': current['token_id']}),
            ('scim_tokens.revoke', credentials.scim_revoke, {'token_id': current['token_id']})]:
        with pytest.raises(HostAPIError) as exc:
            handler(env.call(action, params, user='alice', revision=1))
        assert exc.value.code == 'BUSINESS_ACCESS_DENIED'


# LLM: 读完旧记录之后撤销，写前必须重查token而非重放旧成功。
# 函数用途: 确定性交错验证撤销与last_used的顺序安全。
def test_verify_rechecks_token_after_read(env, monkeypatch):
    call = env.call('scim_tokens.create', {'name': '交错token'})
    data = credentials.scim_create(call)
    original = identity.rows

    # LLM: 使用真实撤销事务制造间隙，不伪造记录或验证结果。
    # 函数用途: 读完立即撤销token。
    def revoke_after_read(call, collection, scope='owner'):
        result = original(call, collection, scope)
        credentials.scim_revoke(env.call('scim_tokens.revoke', {'token_id': data['token']['token_id']}))
        return result

    monkeypatch.setattr(identity, 'rows', revoke_after_read)
    verified = credentials.scim_verify(env.call('scim_tokens.verify', {'secret': data['secret']}))
    assert verified == {'valid': False, 'token_id': None}
    assert identity.one(call, 'scim_tokens', data['token']['token_id'], 'main')['last_used_at'] is None


# LLM: 到期边界精确到秒；主集合和owner集合不能互相替代。
# 函数用途: 核对SCIM过期、散列存储及scope门。
def test_scim_expiry_hash_storage_and_scope(env):
    call = env.call('scim_tokens.create', {'name': '短期', 'expires_in_days': 1})
    data = credentials.scim_create(call)
    row = identity.one(call, 'scim_tokens', data['token']['token_id'], 'main')
    assert row['secret_sha256'] == hashlib.sha256(data['secret'].encode()).hexdigest()
    assert row['owner_id'] == 'local/main'
    with pytest.raises(BusinessError) as exc:
        identity.rows(call, 'scim_tokens', 'owner')
    assert exc.value.code == 'BUSINESS_ACCESS_DENIED'
    env.clock[0] += 86400
    assert credentials.scim_verify(env.call('scim_tokens.verify', {'secret': data['secret']})) == {'valid': False, 'token_id': None}
    assert all(data['secret'].encode() not in path.read_bytes() for path in call.owner_home.rglob('*.json'))


# LLM: 所有候选散列均经constant-time比较，不只在前缀匹配时比较。
# 函数用途: 断言验证路径确实调用compare_digest。
def test_verify_uses_constant_time_comparison(env, monkeypatch):
    first = credentials.scim_create(env.call('scim_tokens.create', {'name': 'one'}))
    credentials.scim_create(env.call('scim_tokens.create', {'name': 'two'}))
    compare, comparisons = credentials.hmac.compare_digest, []

    # LLM: 只记录真实比较输入长度，不改变密码学实现或结果。
    # 函数用途: 观察constant-time比较的调用。
    def observed(left, right):
        comparisons.append((len(left), len(right)))
        return compare(left, right)

    monkeypatch.setattr(credentials.hmac, 'compare_digest', observed)
    assert not credentials.scim_verify(env.call('scim_tokens.verify', {'secret': 'wrong'}))['valid']
    assert comparisons == [(64, 64), (64, 64)]
    assert credentials.scim_verify(env.call('scim_tokens.verify', {'secret': first['secret']}))['valid']


# LLM: 回执和业务日志不能携带api_key，只有原生目录保存写入值。
# 函数用途: 验证原生凭据增改查删及管理员边界。
def test_credentials_upsert_get_masked_delete(env):
    params = {'provider_id': 'custom', 'display_name': '模型服务商', 'api_base': 'http://127.0.0.1:9999/v1',
        'api_key': 'synthetic-owner-secret', 'models': ['model-one']}
    call = env.call('deployment.llm.credentials.upsert', params)
    first = credentials.credentials_upsert(call)
    assert first['provider']['has_secret'] is True and first['provider']['models'] == ['model-one']
    assert credentials.credentials_upsert(call) == first
    read = credentials.credentials_get(env.call('deployment.llm.credentials.get', {'provider_id': 'custom'}))
    assert read['providers'] == [first['provider']]
    update = env.call('deployment.llm.credentials.upsert', {'provider_id': 'custom', 'enabled': False, 'api_key': '***'})
    assert credentials.credentials_upsert(update)['provider']['enabled'] is False
    assert credentials._probe_source(env.call('deployment.llm.credentials.test', {'provider_id': 'custom'}))[1] == params['api_key']
    assert credentials.credentials_get(env.call('deployment.llm.credentials.get', user='alice')) == {'providers': []}
    for action, handler in [('deployment.llm.credentials.upsert', credentials.credentials_upsert),
            ('deployment.llm.credentials.delete', credentials.credentials_delete)]:
        with pytest.raises(HostAPIError) as exc:
            handler(env.call(action, {'provider_id': 'custom'}, user='alice'))
        assert exc.value.code == 'BUSINESS_ACCESS_DENIED'
    collection_dir = call.owner_home / 'data' / 'business_v13'
    assert all(params['api_key'] not in path.read_text() for path in collection_dir.glob('*.json'))
    delete = env.call('deployment.llm.credentials.delete', {'provider_id': 'custom'})
    assert credentials.credentials_delete(delete) == {'provider_id': 'custom', 'deleted': True}
    assert credentials.credentials_delete(delete)['deleted'] is True
    assert credentials.credentials_get(env.call('deployment.llm.credentials.get')) == {'providers': []}
    with pytest.raises(HostAPIError) as exc:
        credentials.credentials_delete(env.call('deployment.llm.credentials.delete', {'provider_id': 'custom'}))
    assert exc.value.code == 'RESOURCE_NOT_FOUND'


# LLM: provider只替代外部网络服务，不替代本地配置、存储或探测业务响应。
# 函数用途: 验证未绑定、成功、失败、重放和模型发现。
def test_probe_actions_require_provider(env):
    save_native(env.call('deployment.llm.status'))
    params = {'provider_id': 'synthetic'}
    for action, handler in [('deployment.llm.credentials.test', credentials.credentials_test),
            ('deployment.llm.credentials.discover_models', credentials.discover_models)]:
        with pytest.raises(HostAPIError) as exc:
            handler(env.call(action, params))
        assert exc.value.code == 'HOST_NOT_READY'
    calls, outcome = [], [True]

    # LLM: 断言传入的是原生目录连接和秘密；返回合成的结构化网络结果。
    # 函数用途: 模拟operator绑定的连通性服务。
    def test(provider, key):
        assert key == 'synthetic-credential-not-real' and provider['api_base'] == 'http://127.0.0.1:9999/v1'
        calls.append('test')
        return {'ok': outcome[0], 'latency_ms': 12.5, 'error_code': None if outcome[0] else 'CONNECTION_REFUSED'}

    # LLM: 模型发现数据不用于覆盖已保存的本地列表。
    # 函数用途: 模拟operator的模型列表服务。
    def list_models(provider, key):
        calls.append('models')
        return ['discovered-model']

    env.hosts['main'].business_v13_providers['llm_probe'] = SimpleNamespace(test=test, list_models=list_models)
    call = env.call('deployment.llm.credentials.test', params)
    assert credentials.credentials_test(call)['ok'] is True
    assert credentials.credentials_test(call)['ok'] is True and calls == ['test']
    outcome[0] = False
    failed = credentials.credentials_test(env.call('deployment.llm.credentials.test', params))
    assert failed['ok'] is False and failed['error_code'] == 'CONNECTION_REFUSED'
    assert credentials.discover_models(env.call('deployment.llm.credentials.discover_models', params))['models'] == ['discovered-model']
    assert credentials.credentials_get(env.call('deployment.llm.credentials.get'))['providers'][0]['models'] == ['test-model']


# LLM: 真正走dispatcher与ready，不把handler直调成功当作网关接线成功。
# 函数用途: 验证绑定provider后运输入口确实可调用探测。
def test_dispatcher_bound_probe(env):
    from agent_py_agent.agent.gateway_parts.business_api import execute_business
    save_native(env.call('deployment.llm.status'))
    env.hosts['main'].business_v13_providers['llm_probe'] = SimpleNamespace(
        test=lambda provider, key: {'ok': False, 'latency_ms': None, 'error_code': 'CONNECTION_REFUSED'},
        list_models=lambda provider, key: ['available-model'])
    for action, expected in [('deployment.llm.credentials.test', {'ok': False}),
            ('deployment.llm.credentials.discover_models', {'models': ['available-model']})]:
        call = env.call(action, {'provider_id': 'synthetic'})
        status, body = execute_business({'action': action, 'request_id': call.request_id,
            'params': call.params, 'idempotency_key': call.idempotency_key}, call.context, call.services)
        assert status == 200, body
        assert body['error'] is None and body['state'] == 'completed'
        assert all(body['data'][key] == value for key, value in expected.items())


# LLM: 使用共享真实Gateway夹具，不修改它的身份映射或注册表。
# 函数用途: 核对HTTP标准信封、生命周期及未绑定探测缺席。
def test_envelopes(v13_native):
    for action in ('deployment.airgap_status', 'deployment.llm.status', 'deployment.llm.credentials.get', 'api_keys.list'):
        status, body = v13_call(v13_native, action, {}, user='credential-alice')
        assert status == 200, body
        assert body['state'] == 'completed' and body['error'] is None
    status, body = v13_call(v13_native, 'system.capabilities.extensions', {}, user='credential-alice')
    assert status == 200, body
    assert 'deployment.llm.credentials.test' not in body['data']['supported_actions']
    assert 'deployment.llm.credentials.discover_models' not in body['data']['supported_actions']
    status, body = v13_call(v13_native, 'deployment.llm.credentials.test', {'provider_id': 'synthetic'},
        user='credential-alice', idempotency_key=uuid4().hex)
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'
    key = uuid4().hex
    status, body = v13_call(v13_native, 'api_keys.create', {'name': 'HTTP key'}, user='credential-alice', idempotency_key=key)
    assert status == 200, body
    data = body['data']
    status, replay = v13_call(v13_native, 'api_keys.create', {'name': 'HTTP key'}, user='credential-alice', idempotency_key=key)
    assert status == 200 and replay['data']['secret'] == '<redacted>'
    status, verified = v13_call(v13_native, 'api_keys.verify', {'secret': data['secret']}, user='credential-alice')
    assert status == 200 and verified['data']['valid'] is True
    status, other = v13_call(v13_native, 'api_keys.verify', {'secret': data['secret']}, user='credential-bob')
    assert status == 200 and other['data']['valid'] is False
    status, revoked = v13_call(v13_native, 'api_keys.revoke', {'key_id': data['key']['key_id']},
        user='credential-alice', idempotency_key=uuid4().hex)
    assert status == 200 and revoked['data']['key']['enabled'] is False
    status, verified = v13_call(v13_native, 'api_keys.verify', {'secret': data['secret']}, user='credential-alice')
    assert status == 200 and verified['data']['valid'] is False
    status, rejected = v13_call(v13_native, 'scim_tokens.list', {}, user='credential-alice')
    assert status == 403 and rejected['error']['code'] == 'BUSINESS_ACCESS_DENIED'
    status, invalid = v13_call(v13_native, 'api_keys.list', {'owner_id': 'local/main'}, user='credential-alice')
    assert status == 400 and invalid['error']['code'] == 'INVALID_SCHEMA'


# LLM: 合成目录也经原生管理器写入，旧客户端允许制造业务上限外的可恢复状态。
# 函数用途: 生成容量测试所需的真实原生目录。
def seed_providers(call, count):
    settings = credentials._settings(call)
    for index in range(count):
        settings.save_provider({'provider_id': 'capacity-' + str(index), 'display_name': '容量测试',
            'api_base': 'http://127.0.0.1:9999/v1', 'models': []})
    return settings


# LLM: 第100条可提交，第101条必须在目录锁内拒绝，拒绝不改字节和回执。
# 函数用途: 验证服务商容量边界在保存前生效。
def test_provider_capacity_admission_before_save(env):
    call = env.call('deployment.llm.credentials.upsert', {'provider_id': 'last',
        'display_name': '最后一条', 'api_base': 'http://127.0.0.1:9999/v1'})
    settings = seed_providers(call, 99)
    assert credentials.credentials_upsert(call)['provider']['provider_id'] == 'last'
    assert len(settings.list_providers({'capability_filter': 'all'})['providers']) == 100
    before = settings.path.read_bytes()
    receipt = call.owner_home / 'data' / 'business_v13' / 'api_keys.json'
    before_receipt = receipt.read_bytes()
    with pytest.raises(HostAPIError) as exc:
        credentials.credentials_upsert(env.call(call.action, {**call.params, 'provider_id': 'overflow'}))
    assert exc.value.code == 'LIMIT_EXCEEDED' and exc.value.outcome == 'not_started'
    assert settings.path.read_bytes() == before and receipt.read_bytes() == before_receipt
    credentials.credentials_upsert(env.call(call.action, {'provider_id': 'last', 'api_key': 'new-synthetic'}))
    assert len(settings.list_providers({'capability_filter': 'all'})['providers']) == 100


# LLM: 超限时不截断列表，删除按锁内目标操作，不依赖全列表投影。
# 函数用途: 验证超限目录仍可通过删除恢复。
@pytest.mark.parametrize('count', [101, 102])
def test_delete_recovers_oversized_native_catalog(env, count):
    call = env.call('deployment.llm.credentials.delete', {'provider_id': 'capacity-0'})
    settings = seed_providers(call, count)
    with pytest.raises(HostAPIError) as exc:
        credentials.credentials_get(replace(call, params={}))
    assert exc.value.code == 'LIMIT_EXCEEDED'
    assert credentials.credentials_delete(call) == {'provider_id': 'capacity-0', 'deleted': True}
    assert credentials.credentials_delete(call)['deleted'] is True
    assert len(settings.list_providers({'capability_filter': 'all'})['providers']) == count - 1
    if count == 102:
        credentials.credentials_delete(env.call(call.action, {'provider_id': 'capacity-1'}))
    assert len(credentials.credentials_get(replace(call, params={}))['providers']) == 100
    with pytest.raises(HostAPIError) as exc:
        credentials.credentials_delete(env.call(call.action, call.params))
    assert exc.value.code == 'RESOURCE_NOT_FOUND'


# LLM: 原生写者在W10等锁期间更新采样参数，凭据补丁必须使用锁内最新profiles。
# 函数用途: 验证密钥更新不覆盖并发修改的模型参数。
@pytest.mark.parametrize('models', [None, ['test-model']])
def test_key_update_preserves_concurrent_native_model_parameters(env, monkeypatch, models):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event
    save_native(env.call('deployment.llm.status'))
    call = env.call('deployment.llm.credentials.upsert', {'provider_id': 'synthetic', 'api_key': 'rotated-synthetic'})
    if models is not None:
        call = replace(call, params={**call.params, 'models': models})
    settings = credentials._settings(call)
    original_lock, waiting, resume = credentials.locked_json_path, Event(), Event()

    # LLM: 仅控制等待目录锁的交错，不替换真实读写。
    # 函数用途: 等原生模型更新后再放行凭据写入。
    @contextmanager
    def wait_before_lock(path):
        waiting.set()
        assert resume.wait(10)
        with original_lock(path):
            yield

    monkeypatch.setattr(credentials, 'locked_json_path', wait_before_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(credentials.credentials_upsert, call)
        try:
            assert waiting.wait(10)
            current = settings.list_providers({'capability_filter': 'all'})['providers'][0]['models'][0]
            settings.save_provider({'provider_id': 'synthetic', 'models': [{**current,
                'temperature': '1.4', 'top_p': 0.6, 'model_context_window_tokens': 8192}]})
        finally:
            resume.set()
        assert future.result(timeout=10)['provider']['has_secret'] is True
    updated = settings.list_providers({'capability_filter': 'all'})['providers'][0]['models'][0]
    assert updated['profile_id'] == current['profile_id']
    assert updated['temperature'] == '1.4' and updated['top_p'] == 0.6
    assert updated['model_context_window_tokens'] == 8192
    assert credentials._probe_source(replace(call, params={'provider_id': 'synthetic'}))[1] == 'rotated-synthetic'


# LLM: 新增最后一个名额与原生并发写者共享目录锁，不能靠锁外数量检查。
# 函数用途: 验证等待期间容量变化仍会阻止超限新增。
def test_capacity_rechecked_after_native_writer(env, monkeypatch):
    from contextlib import contextmanager
    call = env.call('deployment.llm.credentials.upsert', {'provider_id': 'overflow',
        'display_name': '不可保存', 'api_base': 'http://127.0.0.1:9999/v1'})
    settings = seed_providers(call, 99)
    original_lock = credentials.locked_json_path
    saved = []

    # LLM: 先用原生写口占用最后一个名额，再取得原目录锁。
    # 函数用途: 制造容量检查前的确定性交错。
    @contextmanager
    def writer_before_lock(path):
        settings.save_provider({'provider_id': 'native-last', 'display_name': '原生最后一条',
            'api_base': 'http://127.0.0.1:9999/v1', 'models': []})
        saved.append(path.read_bytes())
        with original_lock(path):
            yield

    monkeypatch.setattr(credentials, 'locked_json_path', writer_before_lock)
    with pytest.raises(HostAPIError) as exc:
        credentials.credentials_upsert(call)
    assert exc.value.code == 'LIMIT_EXCEEDED' and settings.path.read_bytes() == saved[0]
    assert len(settings.list_providers({'capability_filter': 'all'})['providers']) == 100


# LLM: 目录准备后撤权必须在原子保存前挡住，admin和live均为写前复核。
# 函数用途: 验证新增和删除在等待后都重查权限。
@pytest.mark.parametrize('gate', ['live', 'admin'])
@pytest.mark.parametrize('deleting', [False, True])
def test_credentials_presave_authorization(env, monkeypatch, gate, deleting):
    from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
    save_native(env.call('deployment.llm.status'))
    action = 'deployment.llm.credentials.' + ('delete' if deleting else 'upsert')
    call = env.call(action, {'provider_id': 'synthetic', **({} if deleting else {'api_key': 'not-saved'})})
    settings = credentials._settings(call)
    before = settings.path.read_bytes()
    read, revoked = credentials.catalog.read_model_profiles, [False]

    # LLM: 只改变实时授权状态，不改变读取的目录数据。
    # 函数用途: 在目录读取后模拟调用者资格失效。
    def revoke_after_catalog_read(path):
        data = read(path)
        revoked[0] = True
        if gate == 'live':
            env.allowed[0] = False
        return data

    require_admin = v13.v13_require_admin

    # LLM: 保留原生管理员校验，只在指定交错注入撤权。
    # 函数用途: 在保存前拒绝已撤销的管理员。
    def current_admin(call):
        if revoked[0] and gate == 'admin':
            raise HostAPIError('BUSINESS_ACCESS_DENIED', 403)
        return require_admin(call)

    monkeypatch.setattr(credentials.catalog, 'read_model_profiles', revoke_after_catalog_read)
    monkeypatch.setattr(v13, 'v13_require_admin', current_admin)
    with pytest.raises(HostAPIError) as exc:
        (credentials.credentials_delete if deleting else credentials.credentials_upsert)(call)
    assert exc.value.code == ('CAPABILITY_DENIED' if gate == 'live' else 'BUSINESS_ACCESS_DENIED')
    assert exc.value.outcome == 'not_started' and settings.path.read_bytes() == before


# LLM: 原生目录已提交后注入投影或回执失败，dispatcher不能回报not_started。
# 函数用途: 验证提交后错误报告unknown且保留实际变更。
@pytest.mark.parametrize('failure', ['projection', 'receipt'])
@pytest.mark.parametrize('deleting', [False, True])
def test_native_commit_followup_failure_outcome(env, monkeypatch, failure, deleting):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.gateway_parts.business_api import execute_business
    save_native(env.call('deployment.llm.status'))
    call = env.call('deployment.llm.credentials.' + ('delete' if deleting else 'upsert'),
        {'provider_id': 'synthetic', **({} if deleting else {'api_key': 'committed-synthetic'})})
    settings = credentials._settings(call)
    before = settings.path.read_bytes()

    # LLM: 只在原生已提交后使业务回执验证失败。
    # 函数用途: 注入一次W0后续回执异常。
    def fail_receipt(self, state, collection):
        raise BusinessError('DOMAIN_RESULT_INVALID')

    # 投影已前移到保存前；提交后故障仍在真实目录提交返回点注入，原断言不变。
    original = credentials._credential_catalog_change

    # LLM: 原目录已真实保存后再抛错，覆盖新增/更新和删除的后续处理失败。
    # 函数用途: 观察提交后故障的结果语义。
    def fail_after_delete(call, progress, *, deleting=False):
        result = original(call, progress, deleting=deleting)
        if failure == 'projection':
            raise HostAPIError('HOST_NOT_READY', 503)
        return result

    monkeypatch.setattr(credentials, '_credential_catalog_change', fail_after_delete)
    if failure == 'receipt':
        monkeypatch.setattr(V13Store, '_validate_state', fail_receipt)
    status, body = execute_business({'action': call.action, 'request_id': call.request_id,
        'params': call.params, 'idempotency_key': call.idempotency_key}, call.context, call.services)
    assert status == 503 and body['error']['code'] == 'OUTCOME_UNKNOWN'
    assert body['state'] == 'failed' and body['effect_outcome'] == 'unknown'
    assert settings.path.read_bytes() != before
    data = credentials.catalog.read_model_profiles(settings.path)
    if deleting:
        assert 'synthetic' not in data['providers'] and not data['profiles']
    else:
        assert data['providers']['synthetic']['api_key'] == 'committed-synthetic'
    assert 'committed-synthetic' not in json.dumps(body)


# LLM: 静态门禁止锁外展示快照回写，读取和保存必须同处原锁。
# 函数用途: 固定窄适配的同锁读写边界。
def test_catalog_adapter_keeps_read_save_under_original_lock():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(credentials._credential_catalog_change))
    locked = [node for node in ast.walk(tree) if isinstance(node, ast.With)
        and any(isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == 'locked_json_path' for item in node.items)]
    assert len(locked) == 1
    calls = [node.func.attr for node in ast.walk(locked[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert 'read_model_profiles' in calls and '_save_profiles' in calls
    assert 'list_providers' not in calls and 'save_provider' not in calls


# LLM: 无身份头时沿共享夹具真实本机管理员认证，不替换HTTP身份和派发。
# 函数用途: 发送本机管理员的真实业务HTTP请求。
def local_http_call(server, action, params, *, key=None):
    import http.client
    request = {'action': action, 'request_id': uuid4().hex, 'params': params}
    if key is not None:
        request['idempotency_key'] = key
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=20)
    try:
        connection.request('POST', '/client/business', json.dumps(request),
            {'Content-Type': 'application/json', 'X-Gateway-Token': server._local_client_credential})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


# LLM: 经真实TCP观察容量边界、掩码读取、重放及超限删除恢复。
# 函数用途: 验证凭据管理边界的HTTP链路。
def test_http_credentials_capacity_delete_recovery(v13_native):
    server, agent = v13_native
    source = SimpleNamespace(context=SimpleNamespace(host=agent), params={})
    settings = seed_providers(source, 99)
    upsert, get, delete = ('deployment.llm.credentials.' + name for name in ('upsert', 'get', 'delete'))
    params = {'provider_id': 'http-last', 'display_name': 'HTTP服务商',
        'api_base': 'http://127.0.0.1:9999/v1', 'api_key': 'http-synthetic-secret', 'models': ['http-model']}
    key = uuid4().hex
    status, first = local_http_call(server, upsert, params, key=key)
    assert status == 200 and first['state'] == 'completed' and first['effect_outcome'] == 'committed', first
    assert first['data']['provider']['has_secret'] is True
    assert 'http-synthetic-secret' not in json.dumps(first)
    before = settings.path.read_bytes()
    status, replay = local_http_call(server, upsert, params, key=key)
    assert status == 200 and replay['data'] == first['data'] and settings.path.read_bytes() == before
    status, overflow = local_http_call(server, upsert, {**params, 'provider_id': 'http-overflow'}, key=uuid4().hex)
    assert status == 400 and overflow['error']['code'] == 'LIMIT_EXCEEDED'
    assert overflow['effect_outcome'] == 'not_started' and settings.path.read_bytes() == before
    status, read = local_http_call(server, get, {})
    assert status == 200 and len(read['data']['providers']) == 100
    settings.save_provider({'provider_id': 'native-extra', 'display_name': '旧客户端额外项',
        'api_base': 'http://127.0.0.1:9999/v1', 'models': []})
    status, oversized = local_http_call(server, get, {})
    assert status == 400 and oversized['error']['code'] == 'LIMIT_EXCEEDED'
    # 列表已超限时更新既有单项不增加数量，也不依赖全列表投影。
    status, updated = local_http_call(server, upsert, {'provider_id': 'http-last', 'api_key': 'rotated-http-secret'}, key=uuid4().hex)
    assert status == 200 and updated['data']['provider']['models'] == ['http-model'], updated
    status, removed = local_http_call(server, delete, {'provider_id': 'native-extra'}, key=uuid4().hex)
    assert status == 200 and removed['data'] == {'provider_id': 'native-extra', 'deleted': True}, removed
    status, read = local_http_call(server, get, {})
    assert status == 200 and len(read['data']['providers']) == 100
    status, removed = local_http_call(server, delete, {'provider_id': 'http-last'}, key=uuid4().hex)
    assert status == 200 and removed['effect_outcome'] == 'committed'
    data = credentials.catalog.read_model_profiles(settings.path)
    assert len(data['providers']) == 99 and not data['profiles']
    status, missing = local_http_call(server, delete, {'provider_id': 'http-last'}, key=uuid4().hex)
    assert status == 404 and missing['error']['code'] == 'RESOURCE_NOT_FOUND'


# LLM: 在真实HTTP写入后破坏投影或回执，必须返回unknown且保留原生提交。
# 函数用途: 验证HTTP提交后故障不被报告为未开始。
@pytest.mark.parametrize('failure', ['projection', 'receipt'])
def test_http_committed_failure_reports_unknown(v13_native, monkeypatch, failure):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    server, agent = v13_native
    original = credentials._credential_catalog_change

    # LLM: 先完成真实原生保存，再注入后续结果处理异常，不以写前拒绝替代提交后故障。
    # 函数用途: 注入一次HTTP原生提交后的错误。
    def fail_projection(call, progress, *, deleting=False):
        original(call, progress, deleting=deleting)
        raise HostAPIError('HOST_NOT_READY', 503)

    # LLM: 只在原生已提交后使业务回执验证失败。
    # 函数用途: 注入一次W0后续回执异常。
    def fail_receipt(self, state, collection):
        raise BusinessError('DOMAIN_RESULT_INVALID')

    if failure == 'projection':
        monkeypatch.setattr(credentials, '_credential_catalog_change', fail_projection)
    else:
        monkeypatch.setattr(V13Store, '_validate_state', fail_receipt)
    params = {'provider_id': 'http-committed', 'display_name': '提交失败观察点',
        'api_base': 'http://127.0.0.1:9999/v1', 'api_key': 'http-committed-synthetic'}
    status, body = local_http_call(server, 'deployment.llm.credentials.upsert', params, key=uuid4().hex)
    assert status == 503 and body['error']['code'] == 'OUTCOME_UNKNOWN', body
    assert body['state'] == 'failed' and body['effect_outcome'] == 'unknown'
    path = credentials.catalog.model_profiles_path(agent.home_paths)
    assert credentials.catalog.read_model_profiles(path)['providers']['http-committed']['api_key'] == params['api_key']
    assert params['api_key'] not in json.dumps(body)


# LLM: 离线环境引用只在临时原生目录制造，不修改真实凭据或产品解析器。
# 函数用途: 将合成服务商迁移为环境引用凭据。
def reference_provider(call, monkeypatch):
    settings = credentials._settings(call)
    data = credentials.catalog.read_model_profiles(settings.path)
    data['providers']['synthetic'].update(api_key='', api_key_ref='env:V13_SYNTHETIC_KEY')
    with credentials.locked_json_path(settings.path):
        credentials.catalog._save_profiles(settings.path, data)
    monkeypatch.setenv('V13_SYNTHETIC_KEY', 'synthetic-environment-value')
    return settings


# LLM: 换端点不可携带原秘密，空值和展示占位符都不算重新授权凭据。
# 函数用途: 验证静态和环境引用凭据的跨端点保护。
@pytest.mark.parametrize('key', [None, '', '***'])
@pytest.mark.parametrize('reference', [False, True])
def test_endpoint_credentials_require_reentry(env, monkeypatch, key, reference):
    save_native(env.call('deployment.llm.status'))
    settings = reference_provider(env.call('deployment.llm.status'), monkeypatch) if reference else credentials._settings(
        env.call('deployment.llm.status'))
    before = settings.path.read_bytes()
    params = {'provider_id': 'synthetic', 'api_base': 'http://127.0.0.2:9999/v1'}
    if key is not None:
        params['api_key'] = key
    with pytest.raises(HostAPIError) as exc:
        credentials.credentials_upsert(env.call('deployment.llm.credentials.upsert', params))
    assert exc.value.code == 'INVALID_SCHEMA' and exc.value.outcome == 'not_started'
    assert settings.path.read_bytes() == before


# LLM: 新静态凭据必须替换而非隐藏在旧环境引用之后，未更新则继续使用同端点引用。
# 函数用途: 验证显式轮换移除旧引用并真实改变解析值。
def test_reference_rotation_uses_new_static_secret(env, monkeypatch):
    save_native(env.call('deployment.llm.status'))
    settings = reference_provider(env.call('deployment.llm.status'), monkeypatch)
    action = 'deployment.llm.credentials.upsert'
    result = credentials.credentials_upsert(env.call(action, {'provider_id': 'synthetic', 'api_key': '***'}))
    assert result['provider']['has_secret'] is True
    assert credentials.credentials_get(env.call('deployment.llm.credentials.get'))['providers'][0]['has_secret'] is True
    assert credentials.llm_status(env.call('deployment.llm.status'))['providers'][0]['has_secret'] is True
    monkeypatch.setenv('V13_SYNTHETIC_KEY', '')
    assert credentials.credentials_get(env.call('deployment.llm.credentials.get'))['providers'][0]['has_secret'] is False
    monkeypatch.setenv('V13_SYNTHETIC_KEY', 'synthetic-environment-value')
    source = env.call('deployment.llm.credentials.test', {'provider_id': 'synthetic'})
    assert credentials._probe_source(source)[1] == 'synthetic-environment-value'
    credentials.credentials_upsert(env.call(action, {'provider_id': 'synthetic', 'api_key': 'synthetic-new-static'}))
    data = credentials.catalog.read_model_profiles(settings.path)
    assert 'api_key_ref' not in data['providers']['synthetic']
    assert credentials._probe_source(source)[1] == 'synthetic-new-static'
    credentials.credentials_upsert(env.call(action, {'provider_id': 'synthetic',
        'api_base': 'http://127.0.0.2:9999/v1', 'api_key': 'synthetic-new-endpoint'}))
    provider, secret = credentials._probe_source(source)
    assert provider['api_base'] == 'http://127.0.0.2:9999/v1' and secret == 'synthetic-new-endpoint'


# LLM: 当前运行和待重启的保存值都受原生embedding保护，拒绝发生在目录保存前。
# 函数用途: 验证更改和删除服务商不能绕过向量模型主机边界。
@pytest.mark.parametrize('source', ['running', 'saved'])
@pytest.mark.parametrize('deleting', [False, True])
def test_embedding_catalog_boundary_before_save(env, source, deleting):
    save_native(env.call('deployment.llm.status'))
    settings = credentials._settings(env.call('deployment.llm.status'))
    model = settings.list_providers({'capability_filter': 'all'})['providers'][0]['models'][0]
    config = env.hosts['main'].config
    if source == 'running':
        config.embedding_model_profile = model['profile_id']
    else:
        config.config_path = str(env.root / 'saved-selection.yaml')
        env.root.joinpath('saved-selection.yaml').write_text('embedding_model_profile: ' + model['profile_id'] + '\n')
    before = settings.path.read_bytes()
    params = {'provider_id': 'synthetic'} if deleting else {'provider_id': 'synthetic',
        'api_base': 'http://127.0.0.2:9999/v1', 'api_key': 'synthetic-explicit-key'}
    with pytest.raises(HostAPIError) as exc:
        (credentials.credentials_delete if deleting else credentials.credentials_upsert)(env.call(
            'deployment.llm.credentials.' + ('delete' if deleting else 'upsert'), params))
    assert exc.value.code == 'BUSINESS_ACCESS_DENIED' and exc.value.outcome == 'not_started'
    assert settings.path.read_bytes() == before


# LLM: 容量按锁内合并后的总量检查，不是仅检查本次补丁长度。
# 函数用途: 验证第501个模型被写前拒绝且已有模型仍可更新。
def test_merged_model_capacity_before_save(env):
    call = env.call('deployment.llm.credentials.upsert', {'provider_id': 'models', 'models': ['additional']})
    settings = credentials._settings(call)
    settings.save_provider({'provider_id': 'models', 'display_name': '模型容量',
        'api_base': 'http://127.0.0.1:9999/v1', 'models': [
            {'model_name': 'model-' + str(index), 'model_backend': 'openai_compatible'} for index in range(500)]})
    before = settings.path.read_bytes()
    with pytest.raises(HostAPIError) as exc:
        credentials.credentials_upsert(call)
    assert exc.value.code == 'LIMIT_EXCEEDED' and exc.value.outcome == 'not_started'
    assert settings.path.read_bytes() == before
    result = credentials.credentials_upsert(env.call(call.action, {'provider_id': 'models', 'models': ['model-0']}))
    assert len(result['provider']['models']) == 500


# LLM: 经真实HTTP复核原生凭据与向量端点保护，不以直调成功替代运输入口。
# 函数用途: 验证新写入口的保护会在网关信封中返回写前拒绝。
def test_http_credentials_native_boundaries(v13_native):
    server, agent = v13_native
    source = SimpleNamespace(context=SimpleNamespace(host=agent), params={})
    save_native(source)
    settings = credentials._settings(source)
    upsert, delete = ('deployment.llm.credentials.' + name for name in ('upsert', 'delete'))
    before = settings.path.read_bytes()
    status, denied = local_http_call(server, upsert, {'provider_id': 'synthetic',
        'api_base': 'http://127.0.0.2:9999/v1', 'api_key': '***'}, key=uuid4().hex)
    assert status == 400 and denied['error']['code'] == 'INVALID_SCHEMA', denied
    assert denied['effect_outcome'] == 'not_started' and settings.path.read_bytes() == before
    model = settings.list_providers({'capability_filter': 'all'})['providers'][0]['models'][0]
    agent.config.embedding_model_profile = model['profile_id']
    for action, params in [(upsert, {'provider_id': 'synthetic',
            'api_base': 'http://127.0.0.2:9999/v1', 'api_key': 'synthetic-explicit'}),
            (delete, {'provider_id': 'synthetic'})]:
        status, denied = local_http_call(server, action, params, key=uuid4().hex)
        assert status == 403 and denied['error']['code'] == 'BUSINESS_ACCESS_DENIED', denied
        assert denied['effect_outcome'] == 'not_started' and settings.path.read_bytes() == before


# LLM: 名称规范化先于稳定ID查找，空白变体不能覆盖参数或占用额外容量。
# 函数用途: 验证满目录与普通目录均复用原生模型行。
@pytest.mark.parametrize('size', [1, 500])
def test_normalized_model_names_preserve_profile(env, size):
    call = env.call('deployment.llm.credentials.upsert', {'provider_id': 'synthetic',
        'models': [' test-model ', 'test-model']})
    settings = credentials._settings(call)
    settings.save_provider({'provider_id': 'synthetic', 'display_name': '名称规范化',
        'api_base': 'http://127.0.0.1:9999/v1', 'models': [
            {'model_name': 'test-model' if index == 0 else 'model-' + str(index),
                'model_backend': 'anthropic_compatible', 'temperature': 0.25, 'top_p': 0.7,
                'model_context_window_tokens': 8192} for index in range(size)]})
    profiles = credentials.catalog.read_model_profiles(settings.path)['profiles']
    result = credentials.credentials_upsert(call)
    assert len(result['provider']['models']) == size
    assert credentials.catalog.read_model_profiles(settings.path)['profiles'] == profiles


# LLM: 原生目录可有超出W10返回合同的字段，保存前应拒绝而非已写入后报告未开始。
# 函数用途: 经真实HTTP验证长模型名和服务商展示名在写前拒绝。
@pytest.mark.parametrize('field', ['model_name', 'display_name'])
def test_http_oversized_native_projection_is_rejected_before_save(v13_native, field):
    server, agent = v13_native
    source = SimpleNamespace(context=SimpleNamespace(host=agent), params={})
    settings = credentials._settings(source)
    settings.save_provider({'provider_id': 'synthetic',
        'display_name': 'x' * 300 if field == 'display_name' else '测试服务商',
        'api_base': 'http://127.0.0.1:9999/v1', 'api_key': 'synthetic-original',
        'models': [{'model_name': 'x' * 300 if field == 'model_name' else 'test-model',
            'model_backend': 'openai_compatible'}]})
    before = settings.path.read_bytes()
    status, result = local_http_call(server, 'deployment.llm.credentials.upsert',
        {'provider_id': 'synthetic', 'api_key': 'synthetic-rotated'}, key=uuid4().hex)
    assert status == 400 and result['error']['code'] == 'INVALID_SCHEMA', result
    assert result['effect_outcome'] == 'not_started'
    assert settings.path.read_bytes() == before
