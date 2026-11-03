"""#89 原生文件/Vault 单元测试，以及不替换分派器的真实 HTTP 集成测试。"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import http.client
import json
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_py_agent.agent.business_spaces import v13_w9_datasources as ds
from agent_py_agent.agent.business_spaces import v13_w9_inbox_oauth as inbox
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.remote_push_service import (
    RemotePushError,
    authenticate_push,
    read_push_json,
    validate_push_payload,
)
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.tests.test_v13_w9_hunting import _job, _model_text, _select_echo_model


# LLM: 这是 F10 的单元输入 DTO，不替代生产分派器；HTTP 测试不使用它。
# 类用途: 固定调用字段以核查产品函数不修改调用身份。
@dataclass(frozen=True)
class UnitCall:
    action: str
    params: dict
    request_id: str
    owner_id: str
    subject_id: str
    context: object
    services: object
    owner_home: Path
    idempotency_key: str
    expected_revision: int | None
    clock: object


# LLM: 仅用于隔离未合入 W0 的事务端口；真实 native tokens 与 Vault 不替换。
# 类用途: 提供单元测试的回调原子性和脱敏回执验证。
class UnitStore:
    # LLM: 每次测试独立，不从真实家目录读取。
    # 函数用途: 初始化单元端口记录。
    def __init__(self):
        self.records, self.receipts, self.locks = {}, {}, {}

    # LLM: 返回副本，防止锁外修改绕过 mutate。
    # 函数用途: 读取单元记录。
    def get(self, collection, key):
        return copy.deepcopy(self.records.get(collection, {}).get(key))

    # LLM: 列表同样只读。
    # 函数用途: 列出单元记录。
    def list(self, collection):
        return copy.deepcopy(list(self.records.get(collection, {}).values()))

    # LLM: 与冻结端口相同，失败不写记录；receipt_view 只影响重放。
    # 函数用途: 执行单元事务。
    def transaction(self, collection, mutate, *, idempotency_key, params_digest, receipt_view=None):
        lock = self.locks.setdefault(collection, threading.RLock())
        with lock:
            key = (collection, idempotency_key)
            if key in self.receipts:
                digest, response = self.receipts[key]
                if digest != params_digest:
                    raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
                return copy.deepcopy(response)
            records = copy.deepcopy(self.records.get(collection, {}))
            result = mutate(records)
            self.records[collection] = records
            self.receipts[key] = (params_digest, copy.deepcopy(receipt_view(result) if receipt_view else result))
            return result


# LLM: 单元替身仅替换 W0 端口；live BusinessCallContext、native 锁与 Vault 均为真实实现。
# 函数用途: 创建明确隔离的单元环境。
@pytest.fixture
def unit_env(tmp_path, monkeypatch):
    root = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(root))
    monkeypatch.setenv('MY_AGENT_HOME', str(root))
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )
    paths = home_paths_with_owner(home_paths(root), resolve_owner_home(root, OwnerIdentity.local_main()))
    host = SimpleNamespace(home_paths=paths, config=SimpleNamespace(business_spaces_enabled=True))
    enabled = [True]
    context = api.bind_business_context(host, authorize=lambda action, facts: enabled[0], subject_id='unit-user')
    store = UnitStore()
    monkeypatch.setattr(ds, '_w9_store', lambda call: store)
    now, provider, jobs = [time.time()], [None], []

    # LLM: 此端口仅记录结构化事实；不能算作真实模型任务生命周期验收。
    # 函数用途: 捕获异步任务提交参数。
    def start_job(call, kind, instruction, *, space_id=None, agent_id=None):
        jobs.append({'kind': kind, 'space_id': space_id, 'instruction': instruction,
                     'facts': json.loads(instruction.split('\n', 1)[1])})
        return {'state': 'accepted', 'job_id': 'unit-job', 'operation_id': 'unit-operation', 'task_id': 'unit-task'}

    # LLM: 冻结端口仍按真实 schema 校验，不接受测试随意增加字段。
    # 函数用途: 提供单元空间和包目录输入。
    def frozen(call, action, params):
        from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
        from agent_py_agent.agent.plugin_host.schemas import validate_object
        validate_object(params, business_action_spec(action).params_schema)
        call.context.check(action, {'owner_id': call.owner_id})
        if action == 'spaces.list':
            return {'items': [{'space_id': 'unit-space'}], 'next_cursor': None}
        return {'items': [], 'next_cursor': None, 'availability': []}

    foundation = SimpleNamespace(v13_resolve_space=lambda call, space_id=None: {'space_id': space_id or 'unit-space'},
        v13_provider=lambda call, name: provider[0],
        v13_call_frozen=frozen,
        v13_start_model_job=start_job)
    monkeypatch.setitem(sys.modules, 'agent_py_agent.agent.gateway_parts.business_api_v13', foundation)

    # LLM: 同 owner 调用绑定同一实时 context；需要跨 owner 测试时显式创建另一 live context。
    # 函数用途: 构造一次单元调用。
    def call(action, params=None, key=None):
        return UnitCall(action, params or {}, 'unit-request', paths.owner_id, 'unit-user', context,
                        api.BusinessServices(host), paths.owner_home_dir,
                        key or ('unit-key-' + hashlib.sha256((action + str(time.monotonic_ns())).encode()).hexdigest()),
                        None, lambda: now[0])

    return SimpleNamespace(call=call, now=now, provider=provider, jobs=jobs, host=host,
                           store=store, root=root, enabled=enabled, foundation=foundation)


# LLM: 签名使用真实正文摘要；每次 nonce 独立，不用固定签名冒充 native 认证。
# 函数用途: 构造原生推送请求。
def _signed(secret, owner, now):
    raw = json.dumps({'target_owner': owner, 'instruction': 'synthetic event',
                      'idempotency_key': 'synthetic-push-one'}).encode()
    stamp, nonce = str(int(now)), 'unit-nonce-1234567890'
    signature = hmac.digest(secret.encode(), f'{stamp}.{nonce}.{hashlib.sha256(raw).hexdigest()}'.encode(), 'sha256').hex()
    return raw, {'X-Agent-Push-Token': secret, 'X-Agent-Timestamp': stamp,
                 'X-Agent-Nonce': nonce, 'X-Agent-Signature': signature}


# LLM: 本模块验证配置与 Vault，不测试 SQLite 磁盘 IO；显式内存规格仍沿原生保存链。
# 函数用途: 保存一条测试数据源。
def _datasource(env, identifier='unit-source'):
    store = ds.w9_datasource_store(env.call('datasources.create'))
    store.save_datasource({'datasource_id': identifier, 'kind': 'sqlite', 'title': '测试连接', 'database_path': ':memory:'})
    return identifier


# LLM: 应用 URL 是合成输入，不发起网络。
# 函数用途: 保存单元 OAuth 配置。
def _oauth_app(env):
    return inbox.upsert_oauth_app(env.call('oauth.app.upsert', {'connector_type': 'unit',
        'client_id': 'synthetic-client', 'client_secret_ref': 'env:W9_TEST_OAUTH_SECRET',
        'authorize_url': 'https://auth.example.test/authorize?state=old&custom=kept',
        'token_url': 'https://auth.example.test/token', 'redirect_uri': 'http://localhost/callback',
        'scopes': ['events.read']}))


# LLM: 模板必须能被原生 validator 消费，而非只断言字段存在。
# 函数用途: 验证内建模板的真实推送协议。
def test_templates_include_builtin_native_push_contract(unit_env):
    result = inbox.list_templates(unit_env.call('inbox.templates.list'))
    template = result['items'][0]
    assert template['template_id'] == 'generic_push_event'
    assert validate_push_payload(template['sample'])['target_owner'] == unit_env.host.home_paths.owner_id
    assert template['signing']['endpoint'] == '/remote-agent/push'
    assert template['body_schema']['additionalProperties'] is False


# LLM: issue 的空参数列表不能抛未知动作，空库读取不写原生 tokens。
# 函数用途: 验证空参数领域读取。
def test_tokens_list_example_empty_params(unit_env):
    assert inbox.list_tokens(unit_env.call('inbox.tokens.list')) == {'items': [], 'coverage': 'complete'}
    assert not (unit_env.root / 'remote_push' / 'tokens.json').exists()


# LLM: 原生文件只存 digest；secret-once 重放由共享事务裁剪。
# 函数用途: 验证签发、原认证、文件权限和幂等冲突。
def test_mint_returns_secret_once_and_native_push_authenticates(unit_env):
    call = unit_env.call('inbox.tokens.mint', {'label': 'unit'}, key='unit-mint-key-0001')
    result = inbox.mint_token(call)
    raw, headers = _signed(result['secret'], call.owner_id, unit_env.now[0])
    row, body, _ = authenticate_push(unit_env.root / 'remote_push', raw, headers, '127.0.0.1', unit_env.now[0])
    assert row['token_id'] == result['token']['token_id']
    assert body['target_owner'] == call.owner_id
    assert inbox.mint_token(call)['secret'] == '<redacted>'
    path = unit_env.root / 'remote_push' / 'tokens.json'
    assert result['secret'] not in path.read_text()
    assert result['secret'] not in repr(unit_env.store.receipts)
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(read_push_json(path.parent, path.name)['tokens']) == 1
    with pytest.raises(HostAPIError, match='IDEMPOTENCY_CONFLICT'):
        inbox.mint_token(replace(call, params={'label': 'other'}))


# LLM: native authenticate_push 在宽限前后都执行，不用仅检查 expires_at 的浅断言。
# 函数用途: 验证轮换宽限与期限耗尽。
def test_rotate_keeps_old_token_valid_for_grace_then_expires(unit_env):
    minted = inbox.mint_token(unit_env.call('inbox.tokens.mint', {'label': 'unit'}))
    call = unit_env.call('inbox.tokens.rotate', {'fingerprint': minted['token']['fingerprint'], 'grace_hours': 1})
    rotated = inbox.rotate_token(call)
    assert rotated['token']['token_id'] != minted['token']['token_id']
    for delta, allowed in ((3599, True), (3600, False)):
        current = unit_env.now[0] + delta
        raw, headers = _signed(minted['secret'], call.owner_id, current)
        if allowed:
            authenticate_push(unit_env.root / 'remote_push', raw, headers, '127.0.0.1', current)
        else:
            with pytest.raises(RemotePushError, match='REMOTE_PUSH_TOKEN_INVALID'):
                authenticate_push(unit_env.root / 'remote_push', raw, headers, '127.0.0.1', current)
    assert inbox.rotate_token(call)['secret'] == '<redacted>'


# LLM: 吊销必须影响原认证，不只改变列表展示。
# 函数用途: 验证即时禁用。
def test_revoke_disables_immediately(unit_env):
    result = inbox.mint_token(unit_env.call('inbox.tokens.mint', {'label': 'unit'}))
    call = unit_env.call('inbox.tokens.revoke', {'fingerprint': result['token']['fingerprint']})
    assert inbox.revoke_token(call)['token']['enabled'] is False
    raw, headers = _signed(result['secret'], call.owner_id, unit_env.now[0])
    with pytest.raises(RemotePushError, match='REMOTE_PUSH_TOKEN_INVALID'):
        authenticate_push(unit_env.root / 'remote_push', raw, headers, '127.0.0.1', unit_env.now[0])
    assert inbox.list_tokens(unit_env.call('inbox.tokens.list'))['items'] == []
    assert len(inbox.list_tokens(unit_env.call('inbox.tokens.list', {'include_disabled': True}))['items']) == 1


# LLM: App 只存宿主引用，普通字符串不能作为引用接受。
# 函数用途: 验证 App 读写及秘密脱敏。
def test_oauth_app_roundtrip_secret_ref_only(unit_env, monkeypatch):
    monkeypatch.setenv('W9_TEST_OAUTH_SECRET', 'synthetic-app-secret')
    result = _oauth_app(unit_env)
    assert result['app']['has_secret'] is True
    assert result['app']['secret_ref_redacted'] == '<redacted>'
    assert 'W9_TEST_OAUTH_SECRET' not in json.dumps(result)
    assert inbox.get_oauth_app(unit_env.call('oauth.app.get', {'connector_type': 'unit'})) == result
    raw = unit_env.call('oauth.app.upsert', {'connector_type': 'unit', 'client_id': 'test',
        'client_secret_ref': 'synthetic-raw-secret', 'authorize_url': 'https://example.test/auth',
        'token_url': 'https://example.test/token', 'redirect_uri': 'http://localhost/callback'})
    with pytest.raises(HostAPIError, match='INVALID_SCHEMA'):
        inbox.upsert_oauth_app(raw)


# LLM: 删除必须影响后续读取，重放不重做删除。
# 函数用途: 验证 App 删除及不存在语义。
def test_oauth_app_delete(unit_env):
    _oauth_app(unit_env)
    call = unit_env.call('oauth.app.delete', {'connector_type': 'unit'})
    assert inbox.delete_oauth_app(call) == {'connector_type': 'unit', 'deleted': True}
    assert inbox.delete_oauth_app(call)['deleted'] is True
    with pytest.raises(HostAPIError, match='RESOURCE_NOT_FOUND'):
        inbox.get_oauth_app(unit_env.call('oauth.app.get', {'connector_type': 'unit'}))


# LLM: 用持久 verifier 计算真实 challenge，不只测 S256 标签。
# 函数用途: 验证 PKCE URL、state 绑定与期限。
def test_oauth_start_builds_pkce_url_and_stores_state(unit_env):
    identifier = _datasource(unit_env)
    _oauth_app(unit_env)
    call = unit_env.call('oauth.start', {'connector_type': 'unit', 'datasource_id': identifier})
    result = inbox.oauth_start(call)
    state = unit_env.store.get('oauth_states', result['state'])
    query = parse_qs(urlsplit(result['authorize_url']).query)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(state['code_verifier'].encode()).digest()).decode().rstrip('=')
    assert query['state'] == [result['state']]
    assert query['code_challenge'] == [challenge]
    assert query['code_challenge_method'] == ['S256'] and query['custom'] == ['kept']
    assert state['owner_id'] == call.owner_id and state['datasource_id'] == identifier
    assert state['used'] is False and state['code_verifier'] not in repr(result)
    assert inbox.oauth_start(call) == result
    from datetime import datetime
    assert datetime.fromisoformat(result['expires_at']).timestamp() == pytest.approx(unit_env.now[0] + 600)


# LLM: provider 仅替换网络边界，Vault 保存读取仍为真实加密实现。
# 函数用途: 验证单次交换、重放和敏感值不进记录。
def test_oauth_complete_exchanges_once_and_stores_tokens_in_vault(unit_env, monkeypatch):
    monkeypatch.setenv('W9_TEST_OAUTH_SECRET', 'synthetic-app-secret')
    identifier = _datasource(unit_env)
    _oauth_app(unit_env)
    started = inbox.oauth_start(unit_env.call('oauth.start', {'connector_type': 'unit', 'datasource_id': identifier}))
    calls = []

    # LLM: 先检查 state 耐久变 used，再返回合成响应。
    # 函数用途: 记录 provider 请求。
    def exchange_code(*, token_url, client_id, client_secret, code, code_verifier, redirect_uri):
        assert unit_env.store.get('oauth_states', started['state'])['used'] is True
        calls.append({'code': code, 'client_secret': client_secret, 'code_verifier': code_verifier})
        return {'access_token': 'synthetic-access-token', 'refresh_token': 'synthetic-refresh-token'}

    unit_env.provider[0] = SimpleNamespace(exchange_code=exchange_code)
    call = unit_env.call('oauth.complete', {'state': started['state'], 'code': 'synthetic-code'})
    result = inbox.oauth_complete(call)
    assert result['connected'] is True and result['datasource_id'] == identifier
    assert inbox.oauth_complete(call) == result
    assert len(calls) == 1 and calls[0]['client_secret'] == 'synthetic-app-secret'
    assert ds.w9_datasource_store(call).resolve_secret(result['credential_ref'])['access_token'] == 'synthetic-access-token'
    for text in (repr(unit_env.store.records), repr(unit_env.store.receipts), repr(result)):
        assert 'synthetic-access-token' not in text and 'synthetic-app-secret' not in text
    assert calls[0]['code_verifier'] not in repr(unit_env.store.receipts)
    with pytest.raises(HostAPIError, match='STATE_CONFLICT'):
        inbox.oauth_complete(replace(call, idempotency_key='different-complete-key'))
    assert len(calls) == 1


# LLM: provider 失败后 state 不可复用；未绑定 provider 不提前消费。
# 函数用途: 验证 provider 门和失败状态不可逆。
def test_oauth_failed_exchange_consumes_state_and_provider_gate_does_not(unit_env, monkeypatch):
    monkeypatch.setenv('W9_TEST_OAUTH_SECRET', 'synthetic-app-secret')
    identifier = _datasource(unit_env)
    _oauth_app(unit_env)
    started = inbox.oauth_start(unit_env.call('oauth.start', {'connector_type': 'unit', 'datasource_id': identifier}))
    call = unit_env.call('oauth.complete', {'state': started['state'], 'code': 'synthetic-code'})
    with pytest.raises(HostAPIError, match='HOST_NOT_READY'):
        inbox.oauth_complete(call)
    assert unit_env.store.get('oauth_states', started['state'])['used'] is False
    calls = []

    # LLM: 异常带合成敏感值，公开异常只能保留固定码。
    # 函数用途: 模拟 provider 连接失败。
    def exchange_code(*, token_url, client_id, client_secret, code, code_verifier, redirect_uri):
        calls.append(code)
        raise RuntimeError('synthetic-sensitive-error')

    unit_env.provider[0] = SimpleNamespace(exchange_code=exchange_code)
    with pytest.raises(HostAPIError, match='HOST_NOT_READY') as failed:
        inbox.oauth_complete(call)
    assert 'synthetic-sensitive-error' not in str(failed.value)
    assert unit_env.store.get('oauth_states', started['state'])['used'] is True
    with pytest.raises(HostAPIError, match='STATE_CONFLICT'):
        inbox.oauth_complete(call)
    assert len(calls) == 1


# LLM: 无效、过期或已用 state 不得调用 provider。
# 函数用途: 验证单次消费边界。
@pytest.mark.parametrize('mode', ['unknown', 'expired', 'used', 'other_owner'])
def test_oauth_state_conflicts(unit_env, mode):
    identifier = _datasource(unit_env)
    _oauth_app(unit_env)
    started = inbox.oauth_start(unit_env.call('oauth.start', {'connector_type': 'unit', 'datasource_id': identifier}))
    state = started['state']
    if mode == 'unknown':
        state = 'unknown-state'
    elif mode == 'expired':
        unit_env.now[0] += 601
    elif mode == 'used':
        unit_env.store.records['oauth_states'][state]['used'] = True
    else:
        unit_env.store.records['oauth_states'][state]['owner_id'] = 'local/other'
    unit_env.provider[0] = SimpleNamespace(exchange_code=lambda: pytest.fail('provider must not run'))
    with pytest.raises(HostAPIError, match='STATE_CONFLICT'):
        inbox.oauth_complete(unit_env.call('oauth.complete', {'state': state, 'code': 'synthetic-code'}))


# LLM: 无绑定不自动签发，刷新沿原轮换逻辑。
# 函数用途: 验证绑定刷新与即时过期。
def test_refresh_push_token_rotates_bound_token_or_404(unit_env):
    identifier = _datasource(unit_env)
    call = unit_env.call('datasources.refresh_push_token', {'datasource_id': identifier, 'grace_hours': 0})
    with pytest.raises(HostAPIError, match='RESOURCE_NOT_FOUND'):
        inbox.refresh_push_token(call)
    minted = inbox.mint_token(unit_env.call('inbox.tokens.mint', {'label': 'bound', 'datasource_id': identifier}))
    refreshed = inbox.refresh_push_token(call)
    assert refreshed['token']['datasource_id'] == identifier
    assert refreshed['token']['token_id'] != minted['token']['token_id']
    raw, headers = _signed(minted['secret'], call.owner_id, unit_env.now[0])
    with pytest.raises(RemotePushError, match='REMOTE_PUSH_TOKEN_INVALID'):
        authenticate_push(unit_env.root / 'remote_push', raw, headers, '127.0.0.1', unit_env.now[0])


# LLM: 另一 owner 即使知道指纹也不能操作。
# 函数用途: 验证原生 token 的严格 owner ACL。
def test_other_owner_cannot_list_rotate_revoke(unit_env):
    minted = inbox.mint_token(unit_env.call('inbox.tokens.mint', {'label': 'private'}))
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )
    paths = home_paths_with_owner(unit_env.host.home_paths, resolve_owner_home(unit_env.root, OwnerIdentity.local_user('other')))
    host = SimpleNamespace(home_paths=paths, config=unit_env.host.config)
    context = api.bind_business_context(host, authorize=lambda action, facts: True)
    call = replace(unit_env.call('inbox.tokens.list'), owner_id=paths.owner_id,
                   owner_home=paths.owner_home_dir, context=context)
    assert inbox.list_tokens(call)['items'] == []
    for action, handler in (('inbox.tokens.rotate', inbox.rotate_token), ('inbox.tokens.revoke', inbox.revoke_token)):
        scoped = replace(call, action=action, params={'fingerprint': minted['token']['fingerprint']},
                         idempotency_key=action.replace('.', '-') + '-other-key')
        with pytest.raises(HostAPIError, match='RESOURCE_NOT_FOUND'):
            handler(scoped)


# LLM: 撤权及 global writer 拒绝均先于副作用。
# 函数用途: 验证 live 授权与全局写门。
def test_live_denial_and_global_writer_have_no_token_side_effect(unit_env, monkeypatch):
    unit_env.enabled[0] = False
    with pytest.raises(HostAPIError, match='CAPABILITY_DENIED'):
        inbox.mint_token(unit_env.call('inbox.tokens.mint', {'label': 'denied'}))
    assert not (unit_env.root / 'remote_push' / 'tokens.json').exists()
    unit_env.enabled[0] = True

    # LLM: 在原产品门出口注入拒绝，不给测试者新增权限。
    # 函数用途: 模拟受限 writer。
    def deny(resource):
        assert resource == 'remote_push_tokens'
        raise RuntimeError('GLOBAL_WRITE_IN_WORKER')

    monkeypatch.setattr(inbox, 'require_global_writer', deny)
    with pytest.raises(RuntimeError, match='GLOBAL_WRITE_IN_WORKER'):
        inbox.mint_token(unit_env.call('inbox.tokens.mint', {'label': 'denied'}))
    assert not (unit_env.root / 'remote_push' / 'tokens.json').exists()


# LLM: 指令/facts 不复制用户症状、DSN、options、Vault 或请求原文。
# 函数用途: 验证诊断任务的脱敏上下文。
def test_troubleshoot_job_context_has_redacted_facts(unit_env):
    identifier = _datasource(unit_env)
    call = unit_env.call('datasources.troubleshoot', {'datasource_id': identifier,
                                                    'symptom': 'Authorization: synthetic-pasted-secret'})
    assert inbox.troubleshoot(call)['state'] == 'accepted'
    job = unit_env.jobs[0]
    assert job['kind'] == 'datasource_troubleshoot'
    assert job['facts']['datasource']['datasource_id'] == identifier
    assert job['facts']['fleet'][0]['status'] == 'unknown'
    assert 'synthetic-pasted-secret' not in repr(job) and 'database_path' not in repr(job)


# LLM: 此夹具不替换 registry、Store、context 或响应，只启本机真实 Gateway 与 echo Agent。
# 函数用途: 提供端到端业务与原生推送入口。
@pytest.fixture
def native_http(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig
    root = tmp_path / 'native-home'
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    monkeypatch.setenv('MY_AGENT_HOME', str(root))
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=False,
        business_spaces_enabled=True, memory_path='memory.jsonl'), tmp_path / 'workspace')
    _select_echo_model(agent)
    agent.business_v13_providers = {}
    server = GatewayHTTPServer(0, gateway_paths_from_root(root / 'gateway'),
                              params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        yield server, agent
    finally:
        server.stop()


# LLM: 用真实客户端发送精确信封，不把测试身份字段放进 params。
# 函数用途: 调用本机 /client/business。
def _business(native_http, action, params, key=None, user='local-agent'):
    server, _ = native_http
    payload = {'action': action, 'request_id': 'wire-request-' + str(time.monotonic_ns()), 'params': params}
    if key:
        payload['idempotency_key'] = key
    headers = {'Content-Type': 'application/json', 'X-Gateway-Token': server._local_client_credential,
               'X-User-Id': user, 'X-Channel': 'chat' if user == 'local-agent' else 'feishu'}
    return _post(server, '/client/business', json.dumps(payload).encode(), headers)


# LLM: 连接生命周期只属于本次请求；不手写服务器回执。
# 函数用途: 发送真实本机 HTTP 并解析响应。
def _post(server, path, raw, headers):
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=20)
    try:
        connection.request('POST', path, raw, headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


# LLM: 空间由真实 W1 创建，不能把单元 DTO 用于 HTTP 集成。
# 函数用途: 创建集成用空间并核验响应。
def _wire_space(native_http):
    status, body = _business(native_http, 'spaces.create', {'name': '收件箱联调'}, 'wire-space-key-0001')
    assert status == 200, body
    return body['data']['space_id']


# LLM: 真实链路必须 200 completed 且闭合信封；缺 W0/注册时测试应失败而不是 skip。
# 函数用途: 验证 issue 空参数样例及模板的实际 HTTP 入口。
def test_envelopes(native_http):
    _wire_space(native_http)
    for action in ('inbox.tokens.list', 'inbox.templates.list'):
        status, body = _business(native_http, action, {})
        assert status == 200, body
        assert body['state'] == 'completed' and body['error'] is None
        assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
                            'allowed_actions', 'warnings', 'effect_outcome'}
        assert body['data']['coverage'] in ('complete', 'partial', 'unknown')


# LLM: 令牌从真实业务端签发，再签真实 body 进原队列，最后查询使用事实并吊销。
# 函数用途: 验证 secret-once 与 /remote-agent/push 的完整原生往返。
def test_wire_mint_push_last_used_rotate_revoke(native_http):
    space_id = _wire_space(native_http)
    params = {'space_id': space_id, 'label': '真实入口测试'}
    key = 'wire-mint-token-key-0001'
    status, body = _business(native_http, 'inbox.tokens.mint', params, key)
    assert status == 200, body
    result = body['data']
    owner = 'local/main'
    raw, headers = _signed(result['secret'], owner, time.time())
    status, receipt = _post(native_http[0], '/remote-agent/push', raw, headers)
    assert status == 202, receipt
    assert receipt['target_owner'] == owner
    status, replay = _business(native_http, 'inbox.tokens.mint', params, key)
    assert status == 200 and replay['data']['secret'] == '<redacted>'
    assert result['secret'] not in json.dumps(replay)
    status, listed = _business(native_http, 'inbox.tokens.list', {})
    assert status == 200, listed
    token = next(row for row in listed['data']['items'] if row['fingerprint'] == result['token']['fingerprint'])
    assert token['last_used_at'] is not None
    status, rotated = _business(native_http, 'inbox.tokens.rotate',
        {'fingerprint': token['fingerprint'], 'grace_hours': 0}, 'wire-rotate-key-0001')
    assert status == 200, rotated
    status, revoked = _business(native_http, 'inbox.tokens.revoke',
        {'fingerprint': rotated['data']['token']['fingerprint']}, 'wire-revoke-key-0001')
    assert status == 200 and revoked['data']['token']['enabled'] is False
    raw, headers = _signed(rotated['data']['secret'], owner, time.time())
    status, refused = _post(native_http[0], '/remote-agent/push', raw, headers)
    assert (status, refused['error_code']) == (401, 'REMOTE_PUSH_TOKEN_INVALID')
    status, other = _business(native_http, 'inbox.tokens.list', {}, user='business-bob')
    assert status == 200 and other['data']['items'] == []


# LLM: App 配置、PKCE、provider 门和 Vault 回执都经真实分派器；只替换外网 provider。
# 函数用途: 验证 OAuth HTTP 闭环及同 owner 单次消费。
def test_wire_oauth_and_refresh_bound_token(native_http, monkeypatch):
    space_id = _wire_space(native_http)
    # SaaS 连接规格不需要本地数据库文件或外网探测。
    status, source = _business(native_http, 'datasources.create', {'space_id': space_id,
        'datasource_id': 'oauth-source', 'name': 'OAuth连接', 'kind': 'https'}, 'wire-source-key-0001')
    assert status == 200, source
    params = {'connector_type': 'wire', 'client_id': 'synthetic-client',
        'client_secret_ref': 'env:W9_WIRE_OAUTH_SECRET', 'authorize_url': 'https://auth.example.test/auth',
        'token_url': 'https://auth.example.test/token', 'redirect_uri': 'http://localhost/callback'}
    monkeypatch.setenv('W9_WIRE_OAUTH_SECRET', 'synthetic-wire-app-secret')
    status, app = _business(native_http, 'oauth.app.upsert', params, 'wire-app-key-0001')
    assert status == 200, app
    assert app['data']['app']['has_secret'] is True
    status, read = _business(native_http, 'oauth.app.get', {'connector_type': 'wire'})
    assert status == 200 and read['data'] == app['data']
    status, started = _business(native_http, 'oauth.start',
        {'connector_type': 'wire', 'datasource_id': 'oauth-source'}, 'wire-start-key-0001')
    assert status == 200, started
    state = started['data']['state']
    status, unbound = _business(native_http, 'oauth.complete', {'state': state, 'code': 'synthetic-code'},
                              'wire-complete-key-0001')
    assert status == 503 and unbound['error']['code'] == 'HOST_NOT_READY'
    calls = []

    # LLM: 仅 provider 网络边界是合成输入，不替换存储或网关行为。
    # 函数用途: 返回合成 OAuth token。
    def exchange_code(*, token_url, client_id, client_secret, code, code_verifier, redirect_uri):
        assert client_secret == 'synthetic-wire-app-secret'
        calls.append(code)
        return {'access_token': 'synthetic-wire-access-token'}

    native_http[1].business_v13_providers['oauth_http'] = SimpleNamespace(exchange_code=exchange_code)
    complete_params = {'state': state, 'code': 'synthetic-code'}
    status, completed = _business(native_http, 'oauth.complete', complete_params, 'wire-complete-key-0001')
    assert status == 200 and completed['data']['connected'] is True, completed
    status, replay = _business(native_http, 'oauth.complete', complete_params, 'wire-complete-key-0001')
    assert status == 200 and replay['data'] == completed['data'] and len(calls) == 1
    status, reused = _business(native_http, 'oauth.complete', complete_params, 'wire-other-complete-key')
    assert status == 409 and reused['error']['code'] == 'STATE_CONFLICT'
    status, missing = _business(native_http, 'datasources.refresh_push_token', {'datasource_id': 'oauth-source'},
                               'wire-refresh-missing-key')
    assert status == 404 and missing['error']['code'] == 'RESOURCE_NOT_FOUND'
    status, minted = _business(native_http, 'inbox.tokens.mint', {'space_id': space_id,
        'label': 'bound', 'datasource_id': 'oauth-source'}, 'wire-bound-token-key')
    assert status == 200, minted
    status, refreshed = _business(native_http, 'datasources.refresh_push_token',
        {'datasource_id': 'oauth-source', 'grace_hours': 0}, 'wire-refresh-bound-key')
    assert status == 200 and refreshed['data']['token']['token_id'] != minted['data']['token']['token_id']
    status, deleted = _business(native_http, 'oauth.app.delete', {'connector_type': 'wire'}, 'wire-delete-app-key')
    assert status == 200 and deleted['data']['deleted'] is True


# LLM: 只替 Echo 输出，任务必须经 Gateway 调度器执行，再由 W0 finalizer/schema 判定终态。
# 函数用途: 验证诊断作业受理、真实执行、脱敏模型上下文及无效结果拒绝。
def test_wire_troubleshoot_accepts_real_job(native_http, monkeypatch):
    space_id = _wire_space(native_http)
    status, source = _business(native_http, 'datasources.create', {'space_id': space_id,
        'datasource_id': 'diagnose-source', 'name': '诊断连接', 'kind': 'https'}, 'wire-diagnose-source-key')
    assert status == 200, source
    output = {'diagnosis': '仅检查已提供的连接事实', 'likely_causes': ['尚无连接测试记录'],
              'suggested_steps': ['检查连接配置'], 'confidence': None}
    observed = _model_text(monkeypatch, output)
    status, accepted = _business(native_http, 'datasources.troubleshoot',
        {'datasource_id': 'diagnose-source', 'symptom': 'synthetic symptom'}, 'wire-troubleshoot-key')
    assert status == 202, json.dumps(accepted, ensure_ascii=False)
    assert accepted['state'] == 'accepted' and accepted['error'] is None
    assert set(accepted['data']) == {'state', 'operation_id', 'job_id', 'task_id'}
    job = _job(native_http, (status, accepted))
    assert job['job_id'] == accepted['data']['job_id'] and job['kind'] == 'datasource_troubleshoot', job
    assert job['state'] == 'completed' and job['result'] == output and job['error'] is None, job
    assert observed and 'diagnose-source' in '\n'.join(observed)
    assert 'synthetic symptom' not in '\n'.join(observed)
    _model_text(monkeypatch, {**output, 'confidence': 2})
    invalid = _job(native_http, _business(native_http, 'datasources.troubleshoot',
        {'datasource_id': 'diagnose-source'}, 'wire-troubleshoot-invalid-key'))
    assert invalid['state'] == 'failed' and invalid['result'] is None, invalid
    assert invalid['error']['code'] == 'JOB_RESULT_INVALID', invalid


# LLM: 同键并发必须只有一次原生副作用与一次 secret-once 响应。
# 函数用途: 验证并发签发的回执一致性。
def test_concurrent_mint_commits_one_native_row(unit_env):
    from concurrent.futures import ThreadPoolExecutor
    call = unit_env.call('inbox.tokens.mint', {'label': 'concurrent'}, key='same-concurrent-mint-key')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(inbox.mint_token, [call, call]))
    assert results[0]['token']['token_id'] == results[1]['token']['token_id']
    assert sum(result['secret'] != '<redacted>' for result in results) == 1
    assert len(read_push_json(unit_env.root / 'remote_push', 'tokens.json')['tokens']) == 1


# LLM: 第一次交换停在真实 provider 边界，第二次不同键不能再次消费同 state。
# 函数用途: 验证 OAuth 并发单次消费与无嵌套事务等待。
def test_concurrent_oauth_state_has_one_exchange(unit_env, monkeypatch):
    monkeypatch.setenv('W9_TEST_OAUTH_SECRET', 'synthetic-app-secret')
    identifier = _datasource(unit_env)
    _oauth_app(unit_env)
    started = inbox.oauth_start(unit_env.call('oauth.start', {'connector_type': 'unit', 'datasource_id': identifier}))
    entered, release = threading.Event(), threading.Event()
    calls, results, errors = [], [], []

    # LLM: Event 交错只阻塞网络端口，不在产品中加锁或改变 state。
    # 函数用途: 保持第一次交换在途。
    def exchange_code(*, token_url, client_id, client_secret, code, code_verifier, redirect_uri):
        calls.append(code)
        entered.set()
        assert release.wait(10)
        return {'access_token': 'synthetic-concurrent-token'}

    unit_env.provider[0] = SimpleNamespace(exchange_code=exchange_code)
    call = unit_env.call('oauth.complete', {'state': started['state'], 'code': 'synthetic-code'})

    # LLM: 线程异常必须交回断言，不让背景失败变成测试成功。
    # 函数用途: 执行第一次完成。
    def run():
        try:
            results.append(inbox.oauth_complete(call))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(10)
        with pytest.raises(HostAPIError, match='STATE_CONFLICT'):
            inbox.oauth_complete(replace(call, idempotency_key='concurrent-second-complete'))
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert len(calls) == 1 and len(results) == 1 and results[0]['connected'] is True


# LLM: 敏感副作用之前的末核不能被 provider 期间撤权绕过。
# 函数用途: 验证 provider 返回后撤权阻止 Vault 写入。
def test_oauth_live_revocation_before_vault_save(unit_env, monkeypatch):
    monkeypatch.setenv('W9_TEST_OAUTH_SECRET', 'synthetic-app-secret')
    identifier = _datasource(unit_env)
    _oauth_app(unit_env)
    started = inbox.oauth_start(unit_env.call('oauth.start', {'connector_type': 'unit', 'datasource_id': identifier}))

    # LLM: provider 完成即撤权，测试准确覆盖写 Vault 前的观察点。
    # 函数用途: 返回 token 并撤销实时访问。
    def exchange_code(*, token_url, client_id, client_secret, code, code_verifier, redirect_uri):
        unit_env.enabled[0] = False
        return {'access_token': 'synthetic-revoked-token'}

    unit_env.provider[0] = SimpleNamespace(exchange_code=exchange_code)
    call = unit_env.call('oauth.complete', {'state': started['state'], 'code': 'synthetic-code'})
    with pytest.raises(HostAPIError, match='CAPABILITY_DENIED'):
        inbox.oauth_complete(call)
    assert not (call.owner_home / 'data' / 'datasource_secrets').exists()
    assert unit_env.store.get('oauth_states', started['state'])['used'] is True
