"""W10身份链路：真实原生认证、当前上下文、持久偏好和HTTP信封。"""
import http.client
import json
from types import SimpleNamespace
from uuid import uuid4

from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_spec
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from agent_py_agent.tests.test_v13_w0_harness import v13_native as v13_native


# LLM: 测试账号仅使用合成密码，写入tmp_path隔离根。
# 函数用途: 创建真实的普通本地账号。
def seed_user(root, user_id='alice'):
    return LocalUserLifecycle(LocalUserStore(root)).create(user_id,
        display_name=user_id.title(), password='Synthetic-password-01!',
        idempotency_key='seed-user-key-0000-' + user_id, actor_owner='local/main').user


# LLM: 上下文通过真实issuer签发；返回封闭业务信封，不绕过dispatcher。
# 函数用途: 创建指定owner的在进程业务客户端。
def client(root, *, user='main', authorize=None, provider=None):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    resolved = (resolve_owner_home(root, OwnerIdentity.provider_user(provider, user)) if provider
        else resolve_owner_home(root) if user == 'main' else LocalUserStore(root).owner_home(user))
    paths = home_paths_with_owner(home_paths(root), resolved)
    host = SimpleNamespace(home_paths=paths, config=SimpleNamespace(business_spaces_enabled=True))
    context = api.bind_business_context(host, authorize=authorize or (lambda action, facts: True), subject_id=user)
    services = api.BusinessServices(host)

    # LLM: 每次都经execute_business校验信封与结果。
    # 函数用途: 执行一个可选择幂等和修订号的动作。
    def invoke(action, params=None, *, key=None, revision=None):
        request = {'action': action, 'request_id': uuid4().hex, 'params': params or {}}
        spec = v13_action_spec(action)
        if spec.requires_idempotency:
            request['idempotency_key'] = key or uuid4().hex
        if revision is not None:
            request['expected_revision'] = revision
        return api.execute_business(request, context, services)

    invoke.host, invoke.context, invoke.services = host, context, services
    return invoke


# LLM: 断言成功必须同时覆盖HTTP、终态、error与真实结果schema。
# 函数用途: 校验业务成功信封。
def success(result):
    status, body = result
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None, body
    return body['data']


# LLM: 错误码和状态均为精确协议断言，禁止以空结果掩盖失败。
# 函数用途: 校验一个失败响应。
def failure(result, status, code):
    actual, body = result
    assert actual == status, body
    assert body['state'] == 'failed' and body['data'] == {}, body
    assert body['error']['code'] == code, body


# LLM: 未配置不能虚构IdP或标记可登录。
# 函数用途: 验证SSO关闭状态。
def test_sso_status_truthful_when_not_configured(tmp_path):
    invoke = client(tmp_path)
    assert success(invoke('auth.sso.status')) == {'enabled': False, 'provider': None,
        'issuer_host': None, 'client_id_masked': None, 'login_supported': False, 'revision': None}


# LLM: 偏好在新实例仍可读取，同键重放不重复写，改参数同键冲突。
# 函数用途: 验证issue的完整偏好示例。
def test_preferences_update_example_then_get(tmp_path):
    invoke = client(tmp_path)
    assert success(invoke('auth.preferences.get'))['preferences']['theme'] == 'system'
    params = {'theme': 'dark', 'default_time_window': '24h', 'locale': 'zh-CN'}
    first = success(invoke('auth.preferences.update', params, key='preferences-key-0001'))
    assert success(invoke('auth.preferences.update', params, key='preferences-key-0001')) == first
    assert success(client(tmp_path)('auth.preferences.get')) == first
    assert first['preferences']['theme'] == 'dark'
    failure(invoke('auth.preferences.update', {'theme': 'light'}, key='preferences-key-0001'), 409, 'IDEMPOTENCY_CONFLICT')
    failure(invoke('auth.preferences.update', {'locale': 'invalid_locale'}), 400, 'INVALID_SCHEMA')
    failure(invoke('auth.preferences.update', {'extra': True}), 400, 'INVALID_SCHEMA')


# LLM: owner隔离和原生admin必须在同一用户投影可见，密码不得出现在响应。
# 函数用途: 验证当前主体、默认偏好与角色。
def test_session_me_principal_roles_preferences(tmp_path):
    seed_user(tmp_path)
    ordinary = client(tmp_path, user='alice')
    me = success(ordinary('auth.session.me'))['me']
    assert me['user_id'] == 'alice' and me['owner_id'] == 'local/alice'
    assert me['is_admin'] is False and me['roles'] == [] and me['email'] is None
    assert me['mfa'] == {'enabled': False, 'required_by_policy': False}
    assert success(client(tmp_path)('auth.session.me'))['me']['roles'] == ['admin']
    success(ordinary('auth.preferences.update', {'theme': 'dark'}))
    assert success(client(tmp_path)('auth.preferences.get'))['preferences']['theme'] == 'system'
    assert 'password' not in json.dumps(me)


# LLM: 原生密码与TOTP均实际验证；回执脱敏且存储不保留明文口令或token。
# 函数用途: 验证登录握手、重放和第二因素。
def test_login_handshake_password_and_totp_and_receipt_redaction(tmp_path):
    import time

    from agent_py_agent.agent.user_space.totp_sso_auth import TotpSSOAuthService, totp_code
    seed_user(tmp_path)
    invoke = client(tmp_path, user='alice')
    params = {'password': 'Synthetic-password-01!', 'ttl_seconds': 60}
    first = success(invoke('auth.session.login', params, key='login-receipt-key-01'))
    assert first['owner_id'] == 'local/alice' and first['session_token'] != '<redacted>'
    second = success(invoke('auth.session.login', params, key='login-receipt-key-01'))
    assert second['session_token'] == '<redacted>'
    data = b''.join(path.read_bytes() for path in (tmp_path / 'owners/local/alice/data/business_v13').glob('*.json'))
    assert first['session_token'].encode() not in data and params['password'].encode() not in data
    failure(invoke('auth.session.login', {'password': 'not-the-password'}), 403, 'VERIFICATION_FAILED')
    service = TotpSSOAuthService(tmp_path)
    setup = service.setup_totp('alice')
    service.confirm_totp('alice', totp_code(setup['secret'], int(time.time()) // 30))
    failure(invoke('auth.session.login', params), 403, 'VERIFICATION_FAILED')
    counter = int(time.time()) // 30
    valid = {totp_code(setup['secret'], counter + offset) for offset in (-1, 0, 1)}
    invalid = next(f'{value:06d}' for value in range(10) if f'{value:06d}' not in valid)
    recovery = {**params, 'totp_code': invalid}
    failure(invoke('auth.session.login', recovery), 403, 'VERIFICATION_FAILED')


# LLM: 故障发生在写动作进入后应保留unknown，不能当作业务成功。
# 函数用途: 验证写入异常的封闭失败信封。
def test_write_failure_and_live_revocation(tmp_path, monkeypatch):
    from agent_py_agent.agent.business_spaces import v13_store
    invoke = client(tmp_path)

    # LLM: 仅注入测试错误，不放宽生产断言。
    # 函数用途: 模拟提交入口不可写。
    def unavailable(self, collection, mutate, *, idempotency_key, params_digest, expected_revision=None, receipt_view=None):
        raise OSError('synthetic')

    monkeypatch.setattr(v13_store.V13Store, 'transaction', unavailable)
    failure(invoke('auth.preferences.update', {'theme': 'dark'}), 503, 'OUTCOME_UNKNOWN')
    denied = client(tmp_path, authorize=lambda action, facts: False)
    failure(denied('auth.preferences.get'), 403, 'CAPABILITY_DENIED')


# LLM: 真实HTTP只携带宿主凭据，不用伪JSON替代Gateway链路。
# 函数用途: 验证当前版本从HTTP入口返回标准信封。
def test_envelopes(v13_native):
    server, agent = v13_native
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=20)
    try:
        connection.request('POST', '/client/business', json.dumps({'action': 'auth.session.me',
            'request_id': 'identity-wire-request', 'params': {}}),
            {'Content-Type': 'application/json', 'X-Gateway-Token': server._local_client_credential})
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200, body
        assert body['state'] == 'completed' and body['error'] is None
        assert body['data']['me']['is_admin'] is True
    finally:
        connection.close()


# LLM: 注册表必须与闭合合同语义一致，不依赖任务文件在运行时存在。
# 函数用途: 验证W10动作数量与所有schema闭包。
def test_registry_w10_contracts_are_closed():
    from agent_py_agent.agent.gateway_parts.business_api_v13_w10 import V13_ACTIONS
    assert len(V13_ACTIONS) == 59
    for definition in V13_ACTIONS:
        spec = v13_action_spec(definition.name)
        assert spec.params_schema['additionalProperties'] is False
        assert spec.result_schema['additionalProperties'] is False
        assert spec.definition.admin == definition.admin


# LLM: 同名外部主体没有原生本地绑定，不能读取本地MFA、展示名或角色。
# 函数用途: 复现并阻止跨命名空间同名身份信息泄露。
def test_external_same_named_subject_never_reads_local_identity(tmp_path):
    from agent_py_agent.agent.user_space.totp_sso_auth import TotpSSOAuthService
    seed_user(tmp_path)
    main = client(tmp_path)
    role = success(main('rbac.roles.create', {'name': 'local-only', 'permissions': ['auth.session.me']}))['role']
    success(main('admin.users.set_roles', {'user_id': 'alice', 'roles': [role['role_id']]}, revision=0))
    TotpSSOAuthService(tmp_path).setup_totp('alice')
    external = client(tmp_path, user='alice', provider='feishu')
    me = success(external('auth.session.me'))['me']
    assert me['owner_id'] != 'local/alice' and me['display_name'] == 'alice'
    assert me['roles'] == [] and me['mfa']['enabled'] is False
    (tmp_path / 'local_users_auth/alice.json').write_text('{broken', encoding='utf-8')
    assert success(external('auth.session.me'))['me'] == me
    effective = success(external('identity.effective_permissions'))
    assert effective['roles'] == [] and effective['permissions'] == [] and effective['is_admin'] is False
    assert effective['providers'] == [{'provider': 'feishu', 'linked': True, 'subject': 'alice'}]
    assert effective['coverage'] == 'partial'
    failure(external('auth.session.login', {'password': 'Synthetic-password-01!'}), 403, 'BUSINESS_ACCESS_DENIED')
