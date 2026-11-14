"""Wave 3 分片 c6 第 1 组整合测试（凭据、遥测、closing 期限、配额、网关负载、有界遍历、路径策略与 runner 停止升级）。"""
from __future__ import annotations

import hashlib
import inspect
import json
import json as receipt_json
import logging
import os
import os as receipt_os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from dataclasses import replace as receipt_replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core import artifact_locator
from agent_py_agent.agent.attempt import sandbox as cache_signal_sb
from agent_py_agent.agent.business_spaces import v13_w10_credentials as credentials
from agent_py_agent.agent.business_spaces import v13_w10_identity as identity
from agent_py_agent.agent.business_spaces import v13_w12_telemetry as telemetry
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.common.json_io import append_private_jsonl_records
from agent_py_agent.agent.contracts import contract_status, real_run_review
from agent_py_agent.agent.conversation import agent_tool_approval as approvals
from agent_py_agent.agent.conversation.background_recovery import StorageWaitBackoff
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.run_claim import (
    ConversationRunLaneRequest,
    conversation_run_lane,
)
from agent_py_agent.agent.conversation.runtime import _BackgroundSchedulerTickMixin
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import closing_deadline as closing
from agent_py_agent.agent.gateway_parts import input_delivery_service as receipt_delivery
from agent_py_agent.agent.gateway_parts import input_delivery_service as rework_inputs
from agent_py_agent.agent.gateway_parts import runtime_file_retention as receipt_retention
from agent_py_agent.agent.gateway_parts import runtime_file_retention as rework_retention
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call, v13_validate_result
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic as receipt_write
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic as rework_write
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root as receipt_paths
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root as rework_paths
from agent_py_agent.agent.gateway_parts.remote_push_service import persist_push_json
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.runtime_db.operations import RuntimeRecoveryRequiredError
from agent_py_agent.agent.runtime_errors import DataCorruptionError as ReworkCorruption
from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
)
from agent_py_agent.agent.subagents import cancellation, result_artifact_evidence
from agent_py_agent.agent.subagents import cancellation_hosts as hosts
from agent_py_agent.agent.tooling import plugin_registration as cache_signal_pr
from agent_py_agent.agent.user_space import owner_quota as quota_module
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaUnavailable
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli import gateway_lane_retry, gateway_loops
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_gateway_agent_control_service import (
    _bound_agent_tree,
    _child_approval_request,
)
from agent_py_agent.tests.test_v13_w0_harness import v13_call, v13_seed, v13_space

pytest_plugins = ('agent_py_agent.tests.test_v13_w0_harness',)
v13_host = harness.v13_host
v13_native = harness.v13_native


# === test_v13_w10_credentials.py: #101：原生模型目录、真实W0持久化生命周期和HTTP入口回归，不触碰真实凭据或网络。 ===

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
def test_credentials_envelopes(v13_native):
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


# === test_v13_w12_telemetry.py: 遥测的真实临时账本、只读边界和原生 HTTP 信封回归，不替生产注册器补接线。 ===

# 复用底座原夹具，不安装插件或构造替代 HTTP 入口。


# LLM: 允许的是测试宿主签发的上下文，不在请求 JSON 伪造 owner。
# 函数用途: 为直接领域调用提供实时授权函数。
def _allow(_action, _facts):
    return True


# LLM: 时钟和身份来自本次隔离宿主，仍使用真实 BusinessCallContext。
# 函数用途: 构造冻结 V13 调用。
def _call(host, *, action='telemetry.funnel.get', params=None, now=None):
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc).timestamp() if now is None else now
    services = api.BusinessServices(host)
    context = api.bind_business_context(host, authorize=_allow, subject_id='telemetry-reader')
    return V13Call(action, {} if params is None else params, 'telemetry-test', host.home_paths.owner_id,
        context.subject_id, context, services, Path(host.home_paths.owner_home_dir), clock=lambda: now)


# LLM: 缺 owner 根会被原 no-follow 守卫拒绝，测试显式创建自己的临时根。
# 函数用途: 提供真实临时 owner 与冻结调用。
@pytest.fixture
def telemetry_call(v13_host):
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    v13_host.home_paths = home_paths_with_owner(v13_host.home_paths, resolve_owner_home(v13_host.home_paths.root))
    v13_host.home_paths.owner_home_dir.mkdir(parents=True, exist_ok=True)
    return _call(v13_host)


# LLM: 对测试树比较正文、模式及修改时间，证明只读查询没有隐藏写入。
# 函数用途: 采集临时树的机械快照。
def _tree(root):
    return {str(path.relative_to(root)): (path.stat().st_mode, path.stat().st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        for path in Path(root).rglob('*')}


# LLM: 通过原推送账本写叶保存合成回执，不发真实推送或网络请求。
# 函数用途: 初始化一份真实 receipts.json。
def _pushes(call, specs):
    root = call.context.host.home_paths.root / 'remote_push'
    root.mkdir(parents=True, exist_ok=True)
    entries = {str(index): {'digest': 'synthetic-digest', 'accepted': accepted,
        'receipt': {'push_id': identifier, 'target_owner': owner, 'queued_at': stamp}}
        for index, (identifier, owner, stamp, accepted) in enumerate(specs)}
    persist_push_json(root / 'receipts.json', {'entries': entries})


# LLM: 调度记录经真实创建事务获得原生 schema、owner、策略和版本。
# 函数用途: 为隔离 owner 创建原生 SchedulerRepository 和任务。
def _scheduler(call):
    paths = call.context.host.home_paths
    repository = SchedulerRepository(call.owner_home / 'data' / 'scheduler',
        owner_provider=paths.owner_provider, owner_kind=paths.owner_kind, owner_id=call.owner_id)
    job, _duplicate = repository.create_job(SchedulerJobCreateRequest(name='遥测测试', prompt='本地合成事件',
        thread_id='thread-telemetry', source_request_id='telemetry-' + uuid4().hex,
        schedule={'kind': 'every', 'every_seconds': 3600, 'anchor_at': call.clock() - 100000, 'timezone': 'UTC'},
        now=call.clock() - 100000))
    return repository, job['job_id']


# LLM: 终态通过原预约、领取、开始与完成链路生成，不手拼假 history 返回值。
# 函数用途: 完成一条真实调度运行并返回持久历史行。
def _run(repository, job_id, ended, *, status='done', wake='wake-one',
         error_code='', delivery_status='', delivery_reason=''):
    run = repository.reserve_manual_run(job_id, now=ended - 3)
    if wake:
        repository.attach_wake_signal(run['run_id'], wake)
    claimed = repository.claim_run(run['run_id'], lease_seconds=60, now=ended - 1)
    assert claimed is not None
    repository.mark_run_running(run['run_id'], claimed['claim_id'], now=ended - 1)
    repository.finish_run(run['run_id'], claimed['claim_id'], SchedulerRunFinish(status=status,
        error_code=error_code, delivery_status=delivery_status, delivery_reason=delivery_reason, now=ended))
    records, errors = repository.history(limit=200)
    assert not errors
    return next(row for row in records if row['run_id'] == run['run_id'])


# LLM: 记录采用 W8 冻结形状，时间和关联字段可独立调整。
# 函数用途: 生成可通过合同校验的临时告警。
def _alert(call, identifier, stamp, *, case_id=None, owner=None, space='soc-default'):
    at = telemetry._at(stamp)
    return dict(alert_id=identifier, space_id=space, title='合成告警', description='', severity='medium',
        status='new', verdict=None, ai_confidence=None, disposition=None, rule_id=None, source='monitor',
        external_ref=None, entities=[], techniques=[], assignee=None, case_id=case_id, tags=[], evidence_refs=[],
        acknowledged_at=None, resolved_at=None, revision=1, created_at=at, updated_at=at,
        owner_id=call.owner_id if owner is None else owner)


# LLM: 脚本文本错误不能冒充错误码，结构化 output.error_code 才参与分组。
# 函数用途: 生成 W7 冻结脚本运行记录。
def _script(call, identifier, stamp, *, code='SCRIPT_TEST_FAILED'):
    at = telemetry._at(stamp)
    return dict(execution_id=identifier, owner_id=call.owner_id, space_id='soc-default', script_id='script-test',
        subject_id='analyst-test', requires_approval=False, task_id=None, context=None, inputs_sha256='0' * 64,
        state='failed', ok=False, output={'error_code': code} if code else None, error='合成脚本错误正文',
        duration_ms=1, started_at=at, finished_at=at, revision=1, created_at=at, updated_at=at)


# LLM: 作业由 W0 集合原形状提供，失败原因来自 error.code。
# 函数用途: 生成临时异步失败作业。
def _job(call, identifier, stamp):
    at = telemetry._at(stamp)
    return dict(job_id=identifier, owner_id=call.owner_id, kind='rule_translation', action='tools.rule_translate',
        subject_id='analyst-test', space_id='soc-default', agent_id='agent-test', task_id=None, operation_id=None,
        state='failed', result=None, error={'code': 'JOB_RESULT_INVALID', 'message': '合成原因'},
        revision=1, created_at=at, updated_at=at)


# LLM: 直接断言真实账本统计、owner/时窗过滤、事件融合和查询零写入。
# 函数用途: 覆盖计划指定的漏斗与缺源语义。
def test_funnel_counts_from_real_ledgers_and_unknown_when_source_missing(telemetry_call):
    call, now = telemetry_call, telemetry_call.clock()
    _pushes(call, [('push-a', call.owner_id, now - 100, True), ('push-b', call.owner_id, now - 50, True),
        ('push-a', call.owner_id, now - 100, True), ('push-other', 'local/other', now - 20, True),
        ('push-old', call.owner_id, now - 86401, True), ('push-future', call.owner_id, now + 1, True)])
    repository, job_id = _scheduler(call)
    _run(repository, job_id, now - 300, wake='shared-event')
    _run(repository, job_id, now - 200, wake='shared-event')
    _run(repository, job_id, now - 100, wake='second-event')
    alerts = [_alert(call, 'alert-a', now - 80, case_id='case-test'), _alert(call, 'alert-b', now - 50),
        _alert(call, 'alert-old', now - 86401), _alert(call, 'alert-future', now + 1),
        _alert(call, 'alert-other', now - 10, owner='local/other')]
    v13_seed(call.owner_home, 'soc_alerts', {row['alert_id']: row for row in alerts})
    before = _tree(call.context.host.home_paths.root)
    result = telemetry.funnel(call)
    assert [row['count'] for row in result['stages']] == [2, 3, 2, 1, 2]
    assert result['reduction_ratio'] == 0
    assert result['unknown'] == [] and result['coverage'] == 'complete'
    assert all(row['source'] for row in result['stages'])
    assert _tree(call.context.host.home_paths.root) == before
    missing_host = SimpleNamespace(home_paths=replace(call.context.host.home_paths,
        root=call.context.host.home_paths.root / 'missing-host',
        owner_home_dir=call.owner_home / 'missing-owner'), config=call.context.host.config)
    missing_host.home_paths.owner_home_dir.mkdir()
    missing = telemetry.funnel(_call(missing_host))
    assert all(row['count'] is None for row in missing['stages'])
    assert missing['unknown'] == ['ingest', 'normalize', 'fuse', 'correlate', 'alert']
    assert missing['coverage'] == 'partial' and missing['reduction_ratio'] is None


# LLM: 空的已初始化来源可报零，未初始化来源必须未知。
# 函数用途: 覆盖空窗、零分母与原只读守卫。
def test_empty_initialized_sources_are_zero_not_missing(telemetry_call):
    call = telemetry_call
    _pushes(call, [])
    _scheduler(call)
    v13_seed(call.owner_home, 'soc_alerts', {})
    result = telemetry.funnel(call)
    assert [stage['count'] for stage in result['stages']] == [0, 0, 0, 0, 0]
    assert result['coverage'] == 'complete' and result['reduction_ratio'] is None


# LLM: 例示空间只是聚合过滤值，阶段来源仍为真实 owner 账本。
# 函数用途: 覆盖原文参数、五阶段吞吐和严格延迟阈值。
def test_pipeline_health_example_params_status_rules(telemetry_call):
    call = replace(telemetry_call, action='telemetry.pipeline.health',
                   params={'space_id': 'soc-default', 'window_hours': 24})
    now = call.clock()
    _pushes(call, [('push-pending', call.owner_id, now - 300, False)])
    repository, job_id = _scheduler(call)
    repository.reserve_manual_run(job_id, now=now - 300)
    v13_seed(call.owner_home, 'soc_alerts', {})
    before = _tree(call.context.host.home_paths.root)
    result = telemetry.pipeline_health(call)
    stages = {stage['id']: stage for stage in result['stages']}
    assert set(stages) == {'ingest', 'normalize', 'fuse', 'correlate', 'alert'}
    assert result['overall'] == 'healthy'
    assert stages['ingest']['lag_ms'] == stages['normalize']['lag_ms'] == 300000
    assert stages['ingest']['eps'] == pytest.approx(1 / 86400)
    assert stages['normalize']['failed'] == 0
    assert stages['ingest']['failed'] is None
    assert all(stages[name]['lag_ms'] is None for name in ('fuse', 'correlate', 'alert'))
    assert _tree(call.context.host.home_paths.root) == before
    later = telemetry.pipeline_health(replace(call, clock=lambda: now + 1))
    assert later['overall'] == 'degraded'
    assert all(row['status'] == 'degraded' for row in later['stages'][:2])


# LLM: 大于五个百分点才降级，测试原生历史投影而非替换聚合器输出。
# 函数用途: 验证失败率边界与 runtime_snapshot 的降级信号。
def test_pipeline_failure_ratio_and_runtime_degraded(telemetry_call):
    call = replace(telemetry_call, action='telemetry.pipeline.health')
    now = call.clock()
    _pushes(call, [])
    repository, job_id = _scheduler(call)
    original = _run(repository, job_id, now - 100)
    copies = [{**original, 'run_id': 'srun_' + uuid4().hex} for _index in range(18)]
    failure = {**original, 'run_id': 'srun_' + uuid4().hex, 'status': 'failed', 'error_code': 'RUN_TEST_FAILED'}
    append_private_jsonl_records(repository.history_path, [*copies, failure])
    v13_seed(call.owner_home, 'soc_alerts', {})
    result = telemetry.pipeline_health(call)
    normalize = result['stages'][1]
    assert normalize['processed'] == 19 and normalize['failed'] == 1
    assert normalize['status'] == 'healthy'
    append_private_jsonl_records(repository.history_path, [{**failure, 'run_id': 'srun_' + uuid4().hex}])
    assert telemetry.pipeline_health(call)['stages'][1]['status'] == 'degraded'
    # 保留健康 history，在原 store 注入一个坏 job，验证原 runtime_snapshot 的健康信号。
    state = json.loads(repository.store_path.read_text())
    state['jobs']['job-invalid'] = {'schema_version': 'invalid'}
    repository.store_path.write_text(json.dumps(state))
    result = telemetry.pipeline_health(call)
    assert result['stages'][1]['status'] == result['stages'][2]['status'] == 'degraded'
    assert result['stages'][1]['reason_code'] == 'SCHEDULER_JOB_INVALID'


# LLM: 四来源分别分组，旧记录和其它空间不计入，HTTP 边缘拒收始终未知。
# 函数用途: 覆盖计划指定的默认二十四小时死信统计。
def test_dead_letters_grouped_by_error_code_last_24h(telemetry_call):
    call = replace(telemetry_call, action='telemetry.dead_letters.list')
    now = call.clock()
    repository, job_id = _scheduler(call)
    _run(repository, job_id, now - 86401, status='failed', error_code='RUN_OLD')
    first = _run(repository, job_id, now - 90, status='failed', error_code='RUN_TEST_FAILED')
    second = _run(repository, job_id, now - 60, status='failed', error_code='RUN_TEST_FAILED',
                  delivery_status='failed', delivery_reason='DELIVERY_TEST_FAILED')
    scripts = [_script(call, 'exec-one', now - 40), _script(call, 'exec-two', now - 30),
               _script(call, 'exec-old', now - 86401)]
    v13_seed(call.owner_home, 'script_runs', {row['execution_id']: row for row in scripts})
    job = _job(call, 'job-model', now - 20)
    v13_seed(call.owner_home, 'async_jobs', {job['job_id']: job})
    before = _tree(call.context.host.home_paths.root)
    result = telemetry.dead_letters(call)
    groups = {(row['source'], row['error_code']): row for row in result['groups']}
    assert {key: row['count'] for key, row in groups.items()} == {
        ('scheduler_run', 'RUN_TEST_FAILED'): 2, ('alert_delivery', 'DELIVERY_TEST_FAILED'): 1,
        ('script_run', 'SCRIPT_TEST_FAILED'): 2, ('async_job', 'JOB_RESULT_INVALID'): 1}
    assert result['total'] == 6
    assert groups['scheduler_run', 'RUN_TEST_FAILED']['first_at'] == telemetry._at(now - 90)
    assert groups['scheduler_run', 'RUN_TEST_FAILED']['sample_refs'] == [second['run_id'], first['run_id']]
    assert result['groups'][0]['source'] == 'async_job'
    assert result['unknown'] == ['remote_push_rejections'] and result['coverage'] == 'partial'
    assert _tree(call.context.host.home_paths.root) == before
    filtered = telemetry.dead_letters(replace(call, params={'source': 'script_run'}))
    assert filtered['total'] == 2 and len(filtered['groups']) == 1
    assert filtered['unknown'] == ['remote_push_rejections']


# LLM: 未持久的错误码和缺来源不能通过通用失败标签补造。
# 函数用途: 验证文本错误、缺源及来源筛选的诚实覆盖。
def test_dead_letters_missing_sources_and_unstructured_error(telemetry_call):
    call = replace(telemetry_call, action='telemetry.dead_letters.list')
    row = _script(call, 'exec-no-code', call.clock() - 10, code=None)
    v13_seed(call.owner_home, 'script_runs', {row['execution_id']: row})
    result = telemetry.dead_letters(call)
    assert result['total'] == 0 and result['groups'] == []
    assert set(result['unknown']) == {'scheduler_run', 'alert_delivery', 'async_job',
                                      'remote_push_rejections', 'script_run_error_code'}
    filtered = telemetry.dead_letters(replace(call, params={'source': 'script_run'}))
    assert filtered['unknown'] == ['remote_push_rejections', 'script_run_error_code']


# LLM: 原生 history 有明确上限，不能将截断投影当全时窗计数。
# 函数用途: 验证二百条覆盖边界和缺融合身份。
def test_history_limit_and_missing_event_identity_are_unknown(telemetry_call):
    call, now = telemetry_call, telemetry_call.clock()
    _pushes(call, [])
    repository, job_id = _scheduler(call)
    row = _run(repository, job_id, now - 100, wake='')
    v13_seed(call.owner_home, 'soc_alerts', {})
    result = telemetry.funnel(call)
    assert result['normalized_count'] == 1 and result['fused_count'] is None
    assert result['unknown'] == ['fuse']
    append_private_jsonl_records(repository.history_path,
        [{**row, 'run_id': 'srun_' + uuid4().hex} for _index in range(199)])
    result = telemetry.funnel(call)
    assert result['normalized_count'] is None and result['fused_count'] is None
    health = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert health['stages'][1]['reason_code'] == 'SCHEDULER_HISTORY_TRUNCATED'


# LLM: pending 不能被查询恢复，冷来源不应创建 data/scheduler 或锁。
# 函数用途: 验证原生只读守卫与明确错误码。
def test_reads_do_not_create_directories_or_recover_pending(telemetry_call):
    call = telemetry_call
    root = call.context.host.home_paths.root
    before = _tree(root)
    health = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert health['overall'] == 'unavailable'
    assert health['stages'][1]['reason_code'] == 'SCHEDULER_SOURCE_MISSING'
    assert _tree(root) == before
    pending = call.owner_home / '.asset-batch.json'
    pending.write_text('{"pending":"synthetic"}')
    before = _tree(root)
    health = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert health['stages'][1]['reason_code'] == 'SCHEDULER_ASSET_RECOVERY_REQUIRED'
    assert pending.read_text() == '{"pending":"synthetic"}'
    assert _tree(root) == before


# LLM: 异常按结构化码而非消息文字分类，不将损坏账本转成健康零。
# 函数用途: 验证坏 JSON 与坏时间的来源不可用结果。
def test_corrupt_sources_preserve_structured_error_codes(telemetry_call):
    call = telemetry_call
    _pushes(call, [])
    receipts = call.context.host.home_paths.root / 'remote_push' / 'receipts.json'
    receipts.write_text('{broken')
    health = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert health['stages'][0]['status'] == 'unavailable'
    assert health['stages'][0]['reason_code'] == 'REMOTE_PUSH_JSON_INVALID'
    row = _alert(call, 'alert-invalid', call.clock())
    row['created_at'] = 'not-a-time'
    v13_seed(call.owner_home, 'soc_alerts', {row['alert_id']: row})
    health = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert health['stages'][4]['processed'] is None
    assert health['stages'][4]['reason_code'] == 'TELEMETRY_TIMESTAMP_INVALID'


# LLM: 不同来源可能不是包含关系，不能为 schema 通过强行钳制缩减率。
# 函数用途: 验证独立流数量不一致的未知比例。
def test_alerts_exceed_ingest_keeps_ratio_unknown(telemetry_call):
    call, now = telemetry_call, telemetry_call.clock()
    _pushes(call, [('push-one', call.owner_id, now - 20, True)])
    _scheduler(call)
    alerts = [_alert(call, 'alert-one', now - 10), _alert(call, 'alert-two', now - 5)]
    v13_seed(call.owner_home, 'soc_alerts', {row['alert_id']: row for row in alerts})
    result = telemetry.funnel(call)
    assert result['reduction_ratio'] is None and result['unknown'] == ['reduction_ratio']
    assert result['coverage'] == 'partial'


# LLM: 实时权限失效不能被遥测的缺源容错吞掉。
# 函数用途: 验证三个动作均保持活跃身份门。
@pytest.mark.parametrize('handler', [telemetry.funnel, telemetry.pipeline_health, telemetry.dead_letters])
def test_permission_denial_is_not_hidden_as_unknown(telemetry_call, handler):
    call = telemetry_call
    context = api.bind_business_context(call.context.host, authorize=lambda _action, _facts: False,
                                       subject_id=call.subject_id)
    with pytest.raises(HostAPIError) as raised:
        handler(replace(call, context=context))
    assert raised.value.code == 'CAPABILITY_DENIED'


# LLM: 真正走 HTTP 鉴权、生产注册器和结果 schema，不在测试中手工注入动作。
# 函数用途: 覆盖计划指定的 completed 信封及真实账本 HTTP 往返。
def test_telemetry_envelopes(v13_native):
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )

    server, agent = v13_native
    space = v13_space(server)
    paths = home_paths_with_owner(agent.home_paths,
        resolve_owner_home(agent.home_paths.root, OwnerIdentity.provider_user('feishu', 'business-alice')))
    host = SimpleNamespace(home_paths=paths, config=agent.config)
    call = _call(host, now=datetime.now(timezone.utc).timestamp())
    _pushes(call, [('push-http', call.owner_id, call.clock() - 30, True)])
    repository, job_id = _scheduler(call)
    _run(repository, job_id, call.clock() - 20, wake='wake-http')
    alert = _alert(call, 'alert-http', call.clock() - 10, space=space, case_id='case-http')
    v13_seed(call.owner_home, 'soc_alerts', {alert['alert_id']: alert})
    v13_seed(call.owner_home, 'script_runs', {})
    v13_seed(call.owner_home, 'async_jobs', {})
    actions = [('telemetry.funnel.get', {'space_id': space}),
        ('telemetry.pipeline.health', {'space_id': 'soc-default', 'window_hours': 24}),
        ('telemetry.dead_letters.list', {})]
    replies = {}
    for action, params in actions:
        status, body = v13_call(server, action, params, user='business-alice')
        assert status == 200, body
        assert body['state'] == 'completed' and body['error'] is None
        assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
                             'allowed_actions', 'warnings', 'effect_outcome'}
        assert body['effect_outcome'] == 'none'
        v13_validate_result(action, body['data'])
        replies[action] = body['data']
    assert [stage['count'] for stage in replies['telemetry.funnel.get']['stages']] == [1, 1, 1, 1, 1]
    assert replies['telemetry.pipeline.health']['overall'] == 'healthy'
    assert replies['telemetry.dead_letters.list']['unknown'] == ['remote_push_rejections']


# LLM: 参数越界在领域调用前失败，显式空间不存在及其它 owner 均不返回空成功。
# 函数用途: 验证真实 HTTP 的闭合参数与精确 404 错误。
def test_http_schema_errors_and_funnel_space_not_found(v13_native):
    server, _agent = v13_native
    space = v13_space(server)
    for action in ('telemetry.funnel.get', 'telemetry.pipeline.health', 'telemetry.dead_letters.list'):
        for params in ({'window_hours': 0}, {'window_hours': 721}, {'window_hours': True}, {'unexpected': 1}):
            status, body = v13_call(server, action, params, user='business-alice')
            assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA', body
            assert body['state'] == 'failed' and body['data'] == {}
    for owner, selected in (('business-alice', 'missing-space'), ('business-bob', space)):
        status, body = v13_call(server, 'telemetry.funnel.get', {'space_id': selected}, user=owner)
        assert status == 404 and body['error']['code'] == 'RESOURCE_NOT_FOUND', body
        assert body['state'] == 'failed' and body['data'] == {}


# LLM: 底座停用必须保持原 503，不让容错聚合绕过 feature gate。
# 函数用途: 验证真实 HTTP 的未就绪错误信封。
def test_http_feature_disabled_is_host_not_ready(v13_native, monkeypatch):
    server, agent = v13_native
    monkeypatch.setattr(agent.config, 'business_spaces_enabled', False)
    for action in ('telemetry.funnel.get', 'telemetry.pipeline.health', 'telemetry.dead_letters.list'):
        status, body = v13_call(server, action, {}, user='business-alice')
        assert status == 503 and body['error']['code'] == 'HOST_NOT_READY', body
        assert body['state'] == 'failed' and body['data'] == {}


# LLM: 字段缺失、重复冲突与极端时间均先单列为未知，不能污染数量统计。
# 函数用途: 验证聚合前的数据质量边界。
@pytest.mark.parametrize('defect', ['missing_case', 'extreme_time', 'conflicting_duplicate'])
def test_alert_data_quality_is_checked_before_aggregation(telemetry_call, defect):
    call = telemetry_call
    row = _alert(call, 'alert-quality', call.clock() - 10)
    if defect == 'missing_case':
        row.pop('case_id')
    elif defect == 'extreme_time':
        row['created_at'] = 10 ** 100
    records = {'first': row}
    if defect == 'conflicting_duplicate':
        records['second'] = {**row, 'case_id': 'case-conflict'}
    v13_seed(call.owner_home, 'soc_alerts', records)
    result = telemetry.funnel(call)
    assert result['alert_count'] is None and result['correlated_count'] is None
    assert result['coverage'] == 'partial'


# LLM: 原生任务记录损坏只降低相应阶段，不能引发属性异常或伪造空融合键。
# 函数用途: 验证 outbox 坏形状的结构化错误。
def test_invalid_delivery_outbox_is_unknown_not_exception(telemetry_call):
    call = telemetry_call
    repository, job_id = _scheduler(call)
    _run(repository, job_id, call.clock() - 10)
    state = json.loads(repository.store_path.read_text())
    state['jobs'][job_id]['delivery_outbox'] = None
    repository.store_path.write_text(json.dumps(state))
    result = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert result['stages'][1]['processed'] == 1
    assert result['stages'][2]['processed'] is None
    assert result['stages'][2]['reason_code'] == 'SCHEDULER_EVENT_KEY_INVALID'


# LLM: 请求允许而单条空间记录被拒绝，不能只依赖工厂的请求级检查。
# 函数用途: 验证受限告警和失败作业不会被遥测聚合绕过。
@pytest.mark.parametrize('source', ['alert', 'script'])
def test_record_level_authorization_is_not_hidden(telemetry_call, source):
    call = telemetry_call
    if source == 'alert':
        row = _alert(call, 'restricted-alert', call.clock() - 10, space='private-space')
        collection, key, handler = 'soc_alerts', 'alert_id', telemetry.funnel
    else:
        row = {**_script(call, 'restricted-script', call.clock() - 10), 'space_id': 'private-space'}
        collection, key, handler = 'script_runs', 'execution_id', telemetry.dead_letters
        call = replace(call, action='telemetry.dead_letters.list', params={'source': 'script_run'})
    v13_seed(call.owner_home, collection, {row[key]: row})
    context = api.bind_business_context(call.context.host, subject_id=call.subject_id,
        authorize=lambda _action, facts: facts.get('space_id') != 'private-space')
    with pytest.raises(HostAPIError) as raised:
        handler(replace(call, context=context))
    assert raised.value.code == 'CAPABILITY_DENIED'


# LLM: 链接指向合法同身份的合成账本也必须拒绝，不能在读取后才校验身份。
# 函数用途: 验证历史来源使用 no-follow 打开而非继承的普通 Path 读取。
def test_history_symlink_is_unavailable(telemetry_call, tmp_path):
    call = telemetry_call
    repository, job_id = _scheduler(call)
    _run(repository, job_id, call.clock() - 10)
    outside = tmp_path / 'synthetic-history.jsonl'
    repository.history_path.rename(outside)
    repository.history_path.symlink_to(outside)
    result = telemetry.pipeline_health(replace(call, action='telemetry.pipeline.health'))
    assert result['stages'][1]['processed'] is None
    assert result['stages'][1]['status'] == 'unavailable'


# === test_v13_w1_closing_deadline_quota.py: W1 closing催收的真实审批/claim交错，不启动模型或生产服务。 ===

# LLM: 夹具保留原owner、RuntimeDB子代理attempt与TaskLink；只构造隔离的Gateway请求，不猜历史会话。
# 函数用途: 为每条回归建立真实审批所属的root和子线程。
def closing_context(tmp_path):
    agent, scope, child = _bound_agent_tree(tmp_path)
    store = agent.conversation_store
    root = store.tasks.load("task-root")
    paths = gateway_paths(agent)
    paths.processing.mkdir(parents=True, exist_ok=True)
    row = {"id": "deadline", "status": "processing", "control_status": "stopping", "turn_phase": "closing",
           "cancel_requested": True, "cancel_requested_at": time.time() - 121, "execution_attempt_id": "transport",
           "conversation_runtime": {"request_id": "deadline", "thread_id": root.thread_id, "task_id": root.task_id},
           "conversation_claim": {"schema_version": "gateway_conversation_claim.v1", "request_id": "deadline",
                                  "task_id": "gateway:deadline", "thread_id": root.thread_id}}
    return SimpleNamespace(agent=agent, scope=scope, store=store, child=child, root=root, paths=paths,
                           path=paths.processing / "deadline.json", row=row)


@pytest.mark.parametrize("real_interrupt", [False, True])
def test_closing_timeout_retires_pending_approval_and_unblocks(tmp_path, monkeypatch, caplog, real_interrupt):
    ctx = closing_context(tmp_path)
    entered, child_returned, executor_entered, allow_exit = (threading.Event() for _ in range(4))
    allow_waiter_read = threading.Event()
    executor_observed_interrupt = threading.Event()
    decisions, failures = [], []
    request = _child_approval_request(ctx.child.id)
    from agent_py_agent.agent.concurrency.interrupt import is_interrupted
    from agent_py_agent.agent.gateway_parts.http_handlers import read_gateway_client_notices

    read_gateway_client_notices(ctx.agent, scope=ctx.scope, after=0, interactive_approvals=True)

    def child_waiter():
        try:
            # publish和wait必须在同一个真实执行线程，不能把原审批回调搬到其它线程。
            handle = approvals.publish_agent_tool_approval(ctx.agent, run_id=ctx.child.id,
                thread_id=ctx.child.agent_thread_id, request_value=request)
            assert handle.scope_valid()

            def valid():
                entered.set()
                assert allow_waiter_read.wait(15)
                return handle.scope_valid()

            decisions.append(approvals.wait_for_agent_tool_approval(replace(handle, scope_valid=valid)))
        except BaseException as exc:
            failures.append(exc)
        finally:
            child_returned.set()

    def executor():
        try:
            with conversation_run_lane(ConversationRunLaneRequest(store=ctx.store, thread_id=ctx.root.thread_id,
                claim_task_id="gateway:deadline", reason="gateway_foreground_turn", lease_seconds=60,
                heartbeat_interval_seconds=10, interrupt_check=lambda: False)) as claim:
                ctx.row["conversation_claim"]["claim_id"] = claim["claim_id"]
                write_json_file(ctx.path, ctx.row)
                executor_entered.set()
                assert child_returned.wait(15)
                if real_interrupt:
                    assert is_interrupted()
                    executor_observed_interrupt.set()
                assert allow_exit.wait(15)
        except BaseException as exc:
            failures.append(exc)

    child_thread, executor_thread = threading.Thread(target=child_waiter), threading.Thread(target=executor)
    # 保留原重发观察，并增加真实命名中断模式：必须由原claim注册把标志送到执行线程。
    interrupt = Mock(return_value=True)
    if not real_interrupt:
        monkeypatch.setattr(closing, "interrupt_by_name", interrupt)
    child_thread.start()
    executor_thread.start()
    try:
        assert entered.wait(3) and executor_entered.wait(3)
        assert not child_returned.is_set(), (decisions, failures)
        result = closing.evaluate_closing_deadlines(ctx.paths, ctx.agent, now=time.time())
        assert result["timed_out"] == 1 and result["retired"] == 1 and result["errors"] == 0, result
        allow_waiter_read.set()
        assert child_returned.wait(3)
        if real_interrupt:
            assert executor_observed_interrupt.wait(3), failures
        assert decisions[0].decision == "cancelled" and decisions[0].feedback == "parent_turn_closing"
        row = read_json_file(ctx.path)
        assert row["closing_deadline"]["code"] == "CLOSING_HARD_TIMEOUT"
        assert row["closing_deadline"]["approval_retirement"]["reason"] == "parent_turn_closing"
        assert row["status"] == "processing" and row["turn_phase"] == "closing"
        # 执行器仍活着时第二个请求绝不能获得会话claim。
        assert executor_thread.is_alive()
        assert ctx.store.claims.acquire({"thread_id": ctx.root.thread_id, "task_id": "gateway:second"}) is None
        assert closing.evaluate_closing_deadlines(ctx.paths, ctx.agent, now=time.time())["timed_out"] == 0
        if not real_interrupt:
            assert interrupt.call_count == 2
    finally:
        allow_waiter_read.set()
        allow_exit.set()
        child_thread.join(3)
        executor_thread.join(3)
    assert not child_thread.is_alive() and not executor_thread.is_alive() and not failures
    # 只有原lane的finally退出后，新请求才能正常进入。
    second = ctx.store.claims.acquire({"thread_id": ctx.root.thread_id, "task_id": "gateway:second"})
    assert second is not None and second["task_id"] == "gateway:second"


# LLM: 六类排除条件在收集后推进本模块墙钟；真实审批夹具与生产期限不变，排除项不得写盘或发中断。
# 函数用途: 验证立即执行和延后超过硬期限时，未过期或非closing请求均保持原样。
@pytest.mark.parametrize("execution_delay", [0.0, 121.0], ids=["immediate", "collection-delayed"])
@pytest.mark.parametrize("change", [
    pytest.param(lambda: {"cancel_requested_at": time.time()}, id="change0"),
    {"turn_phase": "open"}, {"control_status": "running"},
    {"cancel_requested": False}, {"status": "done"}, {"cancel_requested_at": "0"},
])
def test_closing_not_expired_or_nonclosing_is_untouched(tmp_path, change, monkeypatch, execution_delay):
    # 参数已在模块收集时固定；只推进本模块墙钟，不改产品的单调时钟或默认120秒期限。
    execution_now = time.time() + execution_delay
    monkeypatch.setattr(f"{__name__}.time", SimpleNamespace(time=lambda: execution_now))
    ctx = closing_context(tmp_path)
    change = change() if callable(change) else change
    row = {**ctx.row, **change}
    write_json_file(ctx.path, row)
    before = ctx.path.read_bytes()
    interrupt = Mock()
    monkeypatch.setattr(closing, "interrupt_by_name", interrupt)
    assert closing.evaluate_closing_deadlines(ctx.paths, ctx.agent, now=time.time())["timed_out"] == 0
    assert ctx.path.read_bytes() == before
    interrupt.assert_not_called()


def test_retirement_rejects_wrong_parent_thread_and_preserves_prior_decision(tmp_path):
    ctx = closing_context(tmp_path)
    request = _child_approval_request(ctx.child.id)
    handle = approvals.publish_agent_tool_approval(ctx.agent, run_id=ctx.child.id,
        thread_id=ctx.child.agent_thread_id, request_value=request)
    assert approvals.retire_pending_agent_tool_approvals(ctx.agent, thread_id="other", root_task_id="task-root")["retired"] == 0
    assert read_json_file(handle.path)["status"] == "pending"
    from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision

    approvals.resolve_agent_tool_approval(ctx.agent, run_id=ctx.child.id, request_value=request,
        decision_value=ToolApprovalDecision(request.permission_id, "denied"))
    before = handle.path.read_bytes()
    assert approvals.retire_pending_agent_tool_approvals(ctx.agent, thread_id=ctx.root.thread_id, root_task_id="task-root")["retired"] == 0
    assert handle.path.read_bytes() == before


def test_dispatcher_wires_closing_deadline(tmp_path, monkeypatch):
    ctx = closing_context(tmp_path)
    dispatcher = object.__new__(gateway_loops._RequestDispatcher)
    dispatcher.paths, dispatcher.bootstrap_agent = ctx.paths, ctx.agent
    evaluate = Mock()
    monkeypatch.setattr(closing, "evaluate_closing_deadlines", evaluate)
    dispatcher._closing_deadline_once()
    evaluate.assert_called_once()
    assert evaluate.call_args.args == (ctx.paths, ctx.agent)
    assert isinstance(evaluate.call_args.kwargs["now"], float)


# quota_fragment_merged
# LLM: 合并到closing测试时不引入future导入；辅助符号统一quota_前缀，所有文件使用pytest临时目录。
# 模块用途: 验证归档仍计费，扫描/原锁等待可取消且不吞ToolCancelled。


def test_quota_walk_cancels_quickly(tmp_path, monkeypatch):
    archive = tmp_path / 'archive-before-x'
    archive.mkdir()
    for index in range(5000):
        (archive / str(index)).write_bytes(b'abc')
    assert quota_module.owner_logical_usage_bytes(tmp_path, exact=True) == 15000
    original = quota_module.os.walk
    visited = []

    # LLM: 保持真实walk和文件内容，只观察已经访问的目录。
    # 函数用途: 在第一个目录交付后翻转取消谓词。
    def quota_walk(*args, **kwargs):
        for row in original(*args, **kwargs):
            visited.append(row[0])
            yield row

    monkeypatch.setattr(quota_module.os, 'walk', quota_walk)
    with pytest.raises(ToolCancelled):
        quota_module.owner_logical_usage_bytes(tmp_path, exact=True, should_cancel=lambda: bool(visited))
    assert len(visited) <= 2


def test_quota_scan_current_token_is_not_wrapped(tmp_path):
    token = CancellationToken()
    token.cancel()
    with bind_cancellation_token(token):
        with pytest.raises(ToolCancelled):
            quota_module.owner_logical_usage_bytes(tmp_path)
        with pytest.raises(ToolCancelled):
            with quota_module.OwnerQuotaEnforcer(tmp_path, max_bytes=1000).admission():
                pytest.fail('cancelled admission acquired')


def test_quota_lock_wait_is_cancellable(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.json_io import locked_json_path
    waiting, cancel, finished = Event(), Event(), Event()
    failures = []
    original = quota_module.locked_json_path

    # LLM: 仍取得原线程锁/OS锁，仅用Event证明等待者已碰到锁竞争。
    # 函数用途: 观察真实非阻塞锁尝试。
    def quota_lock(*args, **kwargs):
        waiting.set()
        return original(*args, **kwargs)

    # LLM: 线程中记录真实异常并用Event退出，不依赖任意sleep制造交错。
    # 函数用途: 在父线程持锁时等待并接受取消。
    def quota_waiter():
        try:
            with quota_module.OwnerQuotaEnforcer(tmp_path, max_bytes=1000).admission(should_cancel=cancel.is_set):
                failures.append(AssertionError('cancelled waiter acquired lock'))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    with locked_json_path(tmp_path / '.owner-quota'):
        monkeypatch.setattr(quota_module, 'locked_json_path', quota_lock)
        thread = Thread(target=quota_waiter)
        thread.start()
        assert waiting.wait(2)
        cancel.set()
        assert finished.wait(2)
    thread.join(2)
    assert len(failures) == 1 and isinstance(failures[0], ToolCancelled)


def test_approval_retirement_continues_after_invalid_prefix(tmp_path, monkeypatch):
    ctx = closing_context(tmp_path)
    directory = approvals._approval_root(ctx.agent, "task-root")
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(100):
        (directory / f"000-invalid-{index:04}.json").write_text('{}')
    handle = approvals.publish_agent_tool_approval(ctx.agent, run_id=ctx.child.id,
        thread_id=ctx.child.agent_thread_id, request_value=_child_approval_request(ctx.child.id))
    # 真实原生scandir保持插入顺序；每片读32项而不是反复只看排序前缀。
    approvals._retirement_scans.close()
    retired = 0
    for _ in range(8):
        report = approvals.retire_pending_agent_tool_approvals(ctx.agent,
            thread_id=ctx.root.thread_id, root_task_id="task-root", deadline=time.monotonic() + 1)
        retired += report["retired"]
        if retired:
            break
    approvals._retirement_scans.close()
    assert retired == 1
    assert read_json_file(handle.path)["status"] == "decided"


def test_native_scan_budget_counts_non_json_prefix_and_closes(tmp_path, monkeypatch):
    scan = approvals._BoundedJsonDirectoryScan()
    real = approvals.os.scandir
    contexts, visits = [], []

    class Entries:
        def __init__(self, path):
            self.raw = real(path)
            self.read = 0
            self.closed = False
            contexts.append(self)
        def __enter__(self):
            return self
        def __iter__(self):
            return self
        def __next__(self):
            self.read += 1
            return next(self.raw)
        def __exit__(self, *args):
            self.closed = True
            self.raw.close()

    for index in range(100):
        (tmp_path / f"prefix-{index:03}.tmp").touch()
    (tmp_path / 'target.json').write_text('{}')
    monkeypatch.setattr(approvals.os, "scandir", Entries)
    try:
        for page in range(6):
            before = sum(row.read for row in contexts)
            complete = scan.scan(tmp_path, visits.append, max_entries=20, deadline=time.monotonic() + 1)
            assert sum(row.read for row in contexts) - before <= 20
            if complete:
                break
        assert complete and visits == [tmp_path / 'target.json']
        assert len(contexts) == 1 and contexts[0].closed
        before = contexts[0].read
        assert not scan.scan(tmp_path, visits.append, max_entries=20, deadline=time.monotonic() - 1)
        assert contexts[0].read == before
    finally:
        scan.close()


def test_resident_evicted_between_peek_and_pin_is_not_rebuilt(monkeypatch):
    from contextlib import ExitStack

    from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool

    resident, owner = SimpleNamespace(), SimpleNamespace(provider="local", owner_kind="user", owner_id="synthetic")
    key = (owner.provider, owner.owner_kind, owner.owner_id)
    pool = object.__new__(OwnerScopedAgentPool)
    pool._lock, pool._mcp_lifecycle_lock = threading.Lock(), threading.RLock()
    pool._hard_agents, pool._soft_agents = {key: resident}, {}
    pool._pins, pool._retired_agents, pool._last_used = {}, {}, {key: 1.0}
    pool._require_worker_owner = Mock()
    agent = SimpleNamespace(_owner_pool=pool)
    peeked, evicted = threading.Event(), threading.Event()
    original_close = Mock()
    monkeypatch.setattr(pool, '_close_retired_agents', original_close)

    def evict():
        assert peeked.wait(2)
        assert pool.evict_idle(key, resident, 1.0)
        evicted.set()

    worker = threading.Thread(target=evict)
    worker.start()
    with ExitStack() as stack:
        # 原peek已经返回，另一个真实线程完成逐出后才进入新pin路径。
        peeked.set()
        assert evicted.wait(2)
        assert closing._pin_same_resident(agent, owner, resident, stack) is None
    worker.join(2)
    assert not worker.is_alive() and not pool._pins and not pool._hard_agents
    assert original_close.call_count == 1
    # 同一实例仍驻留时，原pin阻止真实逐出，异常退出后由ExitStack正常归还。
    pool._hard_agents[key] = resident
    with pytest.raises(ValueError), ExitStack() as stack:
        assert closing._pin_same_resident(agent, owner, resident, stack) is resident
        assert pool._pins[id(resident)] == 1
        assert not pool.evict_idle(key, resident, 1.0)
        raise ValueError('test unwind')
    assert not pool._pins


def test_closing_without_approval_directory_still_interrupts(tmp_path, monkeypatch):
    ctx = closing_context(tmp_path)
    assert not approvals._approval_root(ctx.agent, "task-root").exists()
    write_json_file(ctx.path, ctx.row)
    approvals._retirement_scans.close()
    interrupt = Mock(return_value=True)
    monkeypatch.setattr(closing, "interrupt_by_name", interrupt)
    result = closing.evaluate_closing_deadlines(ctx.paths, ctx.agent, now=time.time())
    assert result["timed_out"] == 1 and result["errors"] == 0, result
    diagnostic = read_json_file(ctx.path)["closing_deadline"]
    assert diagnostic["code"] == "CLOSING_HARD_TIMEOUT"
    assert diagnostic["approval_retirement"]["complete"] is True
    assert diagnostic["approval_retirement"]["retired"] == 0
    interrupt.assert_called_once()


@pytest.mark.parametrize("failure", [PermissionError("retirement denied"), RuntimeError("retirement failed")])
def test_closing_approval_failure_does_not_suppress_interrupt(tmp_path, monkeypatch, failure):
    ctx = closing_context(tmp_path)
    write_json_file(ctx.path, ctx.row)
    interrupt = Mock(return_value=True)
    monkeypatch.setattr(closing, "interrupt_by_name", interrupt)
    monkeypatch.setattr(closing, "retire_pending_agent_tool_approvals", Mock(side_effect=failure))
    result = closing.evaluate_closing_deadlines(ctx.paths, ctx.agent, now=time.time())
    assert result["timed_out"] == 1 and result["errors"] == 1, result
    diagnostic = read_json_file(ctx.path)["closing_deadline"]
    assert diagnostic["code"] == "CLOSING_HARD_TIMEOUT"
    assert diagnostic["approval_retirement"]["complete"] is False
    assert diagnostic["approval_retirement"]["error_type"] == type(failure).__name__
    assert diagnostic["approval_retirement"]["error"] == str(failure)
    interrupt.assert_called_once()


def test_native_scan_above_capacity_keeps_unfinished_progress(tmp_path, monkeypatch):
    scan = approvals._BoundedJsonDirectoryScan()
    directories, targets, live, peak = [], set(), set(), []
    for number in range(scan._ROOT_LIMIT + 1):
        directory = tmp_path / str(number)
        directory.mkdir()
        directories.append(directory)
        for index in range(65):
            (directory / f"prefix-{index:03}.tmp").touch()
        targets.add(directory / "zz-target.json")
        (directory / "zz-target.json").write_text('{}')

    def ordered_entries(directory):
        # 只在夹具中固定顺序；真实目录、DirEntry和scandir上下文仍由系统提供。
        with approvals.os.scandir(directory) as entries:
            live.add(directory)
            peak.append(len(live))
            try:
                yield from sorted(entries, key=lambda entry: entry.name)
            finally:
                live.remove(directory)

    monkeypatch.setattr(scan, "_entries", ordered_entries)
    visited = set()
    try:
        for _ in range(20):
            for directory in directories:
                scan.scan(directory, visited.add, max_entries=8, deadline=time.monotonic() + 1)
            if targets <= visited:
                break
        assert visited == targets
        assert max(peak) <= scan._ROOT_LIMIT
    finally:
        scan.close()
    assert not live and not scan._slices


def test_closing_pool_lock_contention_does_not_wait_on_peek(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import request_worker
    from agent_py_agent.agent.owner_scoped_pool import OwnerScopedAgentPool
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    ctx = closing_context(tmp_path)
    write_json_file(ctx.path, ctx.row)
    owner = OwnerIdentity.provider_user("synthetic-provider", "synthetic-user")
    pool = object.__new__(OwnerScopedAgentPool)
    pool._lock = threading.Lock()
    pool._hard_agents, pool._soft_agents, pool._pins = {}, {}, {}
    pool._require_worker_owner = Mock()
    pool.peek = Mock(wraps=pool.peek)
    ctx.agent._owner_pool = pool
    monkeypatch.setattr(request_worker, "_resolve_request_owner_identity", lambda *_: owner)
    interrupt = Mock(return_value=True)
    monkeypatch.setattr(closing, "interrupt_by_name", interrupt)
    held, release, finished = (threading.Event() for _ in range(3))
    results, failures = [], []

    def hold_pool():
        with pool._lock:
            held.set()
            assert release.wait(5)

    def tick():
        try:
            results.append(closing.evaluate_closing_deadlines(ctx.paths, ctx.agent, now=time.time()))
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    holder, ticker = threading.Thread(target=hold_pool), threading.Thread(target=tick)
    holder.start()
    assert held.wait(2)
    ticker.start()
    try:
        completed_while_held = finished.wait(1)
    finally:
        release.set()
        holder.join(3)
        ticker.join(3)
    assert not holder.is_alive() and not ticker.is_alive() and not failures
    assert completed_while_held, "closing waited for the owner pool lock"
    pool.peek.assert_not_called()
    assert results[0]["timed_out"] == 1
    assert read_json_file(ctx.path)["closing_deadline"]["approval_retirement"]["complete"] is False
    interrupt.assert_called_once()


# === test_v13_w1_gateway_load_loops.py: W1热循环回归：同owner重建退避、真实入站标记与结构化异常分类。 ===

def test_scheduler_enqueue_backoff_survives_agent_rebuild(tmp_path, caplog):
    calls = Mock(side_effect=OwnerQuotaUnavailable("owner usage reconciliation pending; growth refused"))

    def runtime(owner):
        scheduler = _BackgroundSchedulerTickMixin()
        scheduler.runtime = SimpleNamespace(agent=SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner)))
        scheduler.scheduler_service = SimpleNamespace(reconcile_waiting_runs=lambda **_: None, enqueue_ready_runs=calls)
        return scheduler

    first, second = runtime(tmp_path), runtime(tmp_path)
    with caplog.at_level(logging.WARNING):
        first._enqueue_scheduler_runs(now=100)
        second._enqueue_scheduler_runs(now=100.5)
        assert calls.call_count == 1
        second._enqueue_scheduler_runs(now=101)
        assert calls.call_count == 2
    rows = [row for row in caplog.records if "SCHEDULER_ENQUEUE_BACKOFF" in row.message]
    assert len(rows) == 1
    assert rows[0].exc_info is None and "Traceback" not in caplog.text
    assert first._scheduler_storage_wait is second._scheduler_storage_wait
    runtime(tmp_path / "other")._enqueue_scheduler_runs(now=100)
    assert calls.call_count == 3


def test_shared_storage_wait_is_bounded_and_success_resets(monkeypatch):
    monkeypatch.setattr(StorageWaitBackoff, "_SHARED_LIMIT", 2)
    monkeypatch.setattr(StorageWaitBackoff, "_shared", type(StorageWaitBackoff._shared)())
    first = StorageWaitBackoff.shared("first")
    first.failed(100, OSError("busy"))
    assert StorageWaitBackoff.shared("first") is first and not first.ready(100.5)
    StorageWaitBackoff.shared("second")
    StorageWaitBackoff.shared("third")
    assert len(StorageWaitBackoff._shared) == 2
    assert first.succeeded() and first.ready(100.5)


@pytest.mark.parametrize("error_type", [RuntimeRecoveryRequiredError, ConversationCompactError])
def test_waiting_for_user_input_lane_parks_until_new_message(monkeypatch, caplog, error_type):
    clock = [0.0]
    monkeypatch.setattr(gateway_lane_retry.time, "monotonic", lambda: clock[0])
    retry = gateway_lane_retry.BackgroundLaneRetry()
    marker = ["inbound-1"]
    with caplog.at_level(logging.WARNING):
        error = (error_type("arbitrary non-routing text", code="TEST_COMPACT")
                 if error_type is ConversationCompactError else error_type("arbitrary non-routing text"))
        retry.failed("owner", "thread", error, delay=30,
                     wait_for_inbound_marker=marker[0])
        for now in (30, 300, 3000):
            clock[0] = now
            assert not retry.ready("owner", "thread", model_ready=lambda: True, inbound_marker=lambda: marker[0])
        assert retry._failures[("owner", "thread")].waiting_for_user_input
        marker[0] = "inbound-2"
        assert retry.ready("owner", "thread", model_ready=lambda: True, inbound_marker=lambda: marker[0])
    assert sum("BACKGROUND_LANE_WAITING_FOR_USER_INPUT" in row.message for row in caplog.records) == 1
    retry.failed("owner", "other", ValueError("恢复核实提示尚未交接"), delay=30)
    clock[0] += 29
    assert not retry.ready("owner", "other", model_ready=lambda: True)
    clock[0] += 1
    assert retry.ready("owner", "other", model_ready=lambda: True)


def test_inbound_marker_uses_user_activity_not_background_updates(tmp_path):
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path)
    thread = store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "inbound-test"})
    scheduler = SimpleNamespace(store=store)
    original = gateway_loops._thread_inbound_marker(scheduler, thread.thread_id)
    store.threads.update_summary(thread.thread_id, summary="background summary")
    assert gateway_loops._thread_inbound_marker(scheduler, thread.thread_id) == original
    store.threads.record_user_activity(thread.thread_id, request_id="inbound-new")
    assert gateway_loops._thread_inbound_marker(scheduler, thread.thread_id) != original


def test_thread_tick_captures_marker_before_execution_and_does_not_print_programmer_bug(monkeypatch):
    supervisor = object.__new__(gateway_loops._BackgroundThreadLaneSupervisorMixin)
    supervisor.stop_event = SimpleNamespace(is_set=lambda: False)
    supervisor._lane_retry = gateway_lane_retry.BackgroundLaneRetry()
    marker = ["before"]
    monkeypatch.setattr(gateway_loops, "_thread_inbound_marker", lambda *_: marker[0])
    printer = Mock()
    monkeypatch.setattr(gateway_loops, "_print_gateway_loop_error", printer)

    def execute(*_, **__):
        marker[0] = "arrived-during-execution"
        raise RuntimeRecoveryRequiredError("unrelated text")

    assert supervisor._safe_thread_tick(SimpleNamespace(tick_thread=execute), "owner", "thread") == []
    printer.assert_not_called()
    assert supervisor._lane_retry.ready("owner", "thread", model_ready=lambda: True, inbound_marker=lambda: marker[0])


def test_unknown_marker_never_unparks_by_time():
    retry = gateway_lane_retry.BackgroundLaneRetry()
    retry.failed("o", "t", RuntimeRecoveryRequiredError("x"), delay=0)
    assert not retry.ready("o", "t", model_ready=lambda: True)
    assert not retry.ready("o", "t", model_ready=lambda: True, inbound_marker=lambda: "first-valid")
    assert not retry.ready("o", "t", model_ready=lambda: True, inbound_marker=lambda: "first-valid")
    assert retry.ready("o", "t", model_ready=lambda: True, inbound_marker=lambda: "new")


# --- 缓存失效和SIGUSR1安全导出的独立回归，供父级合入负载测试。 ---


def cache_signal_spec(root):
    return cache_signal_sb.AttemptSandboxSpec(
        attempt_view=root, staging_root=root, shared_workspace=root, owner_home=root,
        implicit_attempt_write_roots=False,
    )


@pytest.fixture
def cache_signal_empty_cache():
    cache_signal_sb._MacosGitMarkerCache.entries.clear()
    yield
    cache_signal_sb._MacosGitMarkerCache.entries.clear()


def test_git_marker_cache_hits_and_invalidates_on_mtime(tmp_path, monkeypatch, cache_signal_empty_cache):
    root = tmp_path / "root"
    child = root / "child"
    nested = child / "nested"
    nested.mkdir(parents=True)
    (nested / ".git").mkdir()
    clock = [100.0]
    monkeypatch.setattr(cache_signal_sb, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original, scans = cache_signal_sb.os.scandir, []

    def cache_signal_scan(path):
        scans.append(Path(path))
        return original(path)

    monkeypatch.setattr(cache_signal_sb.os, "scandir", cache_signal_scan)
    spec = cache_signal_spec(root)
    first = cache_signal_sb._macos_git_markers(spec)
    assert nested / ".git" in first and scans
    scans.clear()
    assert cache_signal_sb._macos_git_markers(spec) == first
    assert scans == []
    stamp = nested.stat().st_mtime_ns
    os.utime(nested, ns=(stamp + 1_000_000, stamp + 1_000_000))
    assert cache_signal_sb._macos_git_markers(spec) == first
    assert nested in scans
    scans.clear()
    clock[0] += 5.0
    assert cache_signal_sb._macos_git_markers(spec) == first
    assert scans
    scans.clear()
    new_git = child / ".git"
    new_git.mkdir()
    assert new_git in cache_signal_sb._macos_git_markers(spec)
    assert scans


def test_git_marker_cache_realpath_root_isolation_and_bounds(tmp_path, monkeypatch, cache_signal_empty_cache):
    original, scans = cache_signal_sb.os.scandir, []

    def cache_signal_scan(path):
        scans.append(Path(path))
        return original(path)

    root = tmp_path / "first"
    (root / "a" / "b" / "c" / "d" / ".git").mkdir(parents=True)
    (root / "a" / "b" / "c" / ".git").mkdir()
    (root / "node_modules" / ".git").mkdir(parents=True)
    monkeypatch.setattr(cache_signal_sb.os, "scandir", cache_signal_scan)
    markers = cache_signal_sb._macos_git_markers(cache_signal_spec(root))
    assert root / "a" / "b" / "c" / ".git" in markers
    assert root / "a" / "b" / "c" / "d" / ".git" not in markers
    assert root / "node_modules" not in scans
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    scans.clear()
    assert cache_signal_sb._macos_git_markers(cache_signal_spec(alias)) == markers
    assert scans == []
    second = tmp_path / "second"
    second.mkdir()
    cache_signal_sb._macos_git_markers(cache_signal_spec(second))
    assert scans == [second]
    wide = tmp_path / "wide"
    wide.mkdir()
    for index in range(140):
        (wide / f"dir-{index:03}").mkdir()
    scans.clear()
    cache_signal_sb._macos_git_markers(cache_signal_spec(wide))
    assert len(scans) == 128
    for index in range(70):
        another = tmp_path / f"root-{index}"
        another.mkdir()
        cache_signal_sb._macos_git_markers(cache_signal_spec(another))
    assert len(cache_signal_sb._MacosGitMarkerCache.entries) == 64


def test_git_marker_cache_does_not_hide_scan_errors(tmp_path, monkeypatch, cache_signal_empty_cache):
    def cache_signal_denied(path):
        raise PermissionError("synthetic denied")

    monkeypatch.setattr(cache_signal_sb.os, "scandir", cache_signal_denied)
    with pytest.raises(PermissionError):
        cache_signal_sb._macos_git_markers(cache_signal_spec(tmp_path))
    assert cache_signal_sb._MacosGitMarkerCache.entries == {}


@pytest.mark.parametrize("error_type", [ValueError, FileNotFoundError])
@pytest.mark.parametrize("retry_seconds", [0.0, 30.0])
def test_plugin_registration_value_error_warns_once(tmp_path, monkeypatch, caplog, error_type, retry_seconds):
    owner = SimpleNamespace(home_dir=tmp_path, plugins_dir=tmp_path / "plugins", identity=OwnerIdentity.local_main())
    installation = SimpleNamespace(enabled=True, activation=None, revision="revision",
                                   activation_id="activation", manifest=SimpleNamespace(
                                       plugin_id="plugin", is_content_only=False))
    registry = SimpleNamespace(_construction_params=SimpleNamespace(
        plugin_owner=owner, host_credential_env_names=frozenset()),
        _mcp_clients=[], _mcp_retry_state={}, _mcp_closed=threading.Event())
    attempts = []

    class cache_signal_BrokenClient:
        def __init__(self, *args, **kwargs):
            attempts.append(True)
            raise error_type("synthetic invalid context")

    monkeypatch.setattr(cache_signal_pr, "PluginMCPClient", cache_signal_BrokenClient)
    monkeypatch.setattr(cache_signal_pr, "_discover_plugin_manifests", lambda _: ((installation,), ()))
    monkeypatch.setattr(cache_signal_pr, "_MISSING_ENV_RETRY_SECONDS", retry_seconds)
    monkeypatch.setattr(cache_signal_pr, "_MISSING_ENVIRONMENTS", cache_signal_pr.OrderedDict())
    monkeypatch.setattr(cache_signal_pr, "_MISSING_ENV_WARNED", cache_signal_pr.OrderedDict())
    with caplog.at_level(logging.DEBUG, logger=cache_signal_pr.__name__):
        for _ in range(5):
            cache_signal_pr.synchronize_plugin_clients(registry)
    records = [r for r in caplog.records if r.name == cache_signal_pr.__name__]
    assert len([r for r in records if r.levelno == logging.WARNING]) == 1
    assert len([r for r in records if r.levelno == logging.DEBUG]) == 4
    assert len(attempts) == (1 if retry_seconds else 5)
    assert error_type.__name__ in records[0].getMessage()
    assert "synthetic invalid context" in records[0].getMessage()
    installation.activation_id = "new-activation"
    with caplog.at_level(logging.WARNING, logger=cache_signal_pr.__name__):
        cache_signal_pr.synchronize_plugin_clients(registry)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="需要POSIX SIGUSR1")
def test_sigusr1_python_safe_dump(tmp_path):
    source = r'''
import faulthandler, json, os, signal, sys, threading
from pathlib import Path
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli import gateway_process as gp
sys.setswitchinterval(0.001)
register_calls = []
faulthandler.register = lambda *a, **k: register_calls.append(a)
dumped = threading.Event()
class StderrObserver:
    has_dump = False
    def fileno(self):
        return sys.__stderr__.fileno()
    def write(self, text):
        if text.startswith("Gateway SIGUSR1 Python thread dump"):
            self.has_dump = True
        return sys.__stderr__.write(text)
    def flush(self):
        sys.__stderr__.flush()
        if self.has_dump:
            dumped.set()
sys.stderr = StderrObserver()
paths = gateway_paths_from_root(Path(sys.argv[1]))
old_handler = signal.getsignal(signal.SIGUSR1)
previous = gp._install_gateway_signal_handlers(paths, {"pid": os.getpid()})
assert signal.getsignal(signal.SIGUSR1) is previous.diagnostic_handler
assert previous.stack_dumper.thread.daemon
stop, ready = threading.Event(), threading.Barrier(51)
def busy():
    ready.wait(timeout=20)
    while not stop.is_set():
        value = Path("synthetic") / "busy" / "path"
        value.parts
workers = [threading.Thread(target=busy, name=f"cache-signal-busy-{i:02}") for i in range(50)]
try:
    for worker in workers:
        worker.start()
    ready.wait(timeout=20)
    os.kill(os.getpid(), signal.SIGUSR1)
    assert dumped.wait(20), "dumper did not flush"
    assert all(worker.is_alive() for worker in workers)
finally:
    stop.set()
    for worker in workers:
        worker.join(5)
    gp._restore_gateway_signal_handlers(previous)
assert signal.getsignal(signal.SIGUSR1) == old_handler
assert not previous.stack_dumper.thread.is_alive()
assert not any(worker.is_alive() for worker in workers)
assert register_calls == []
print(json.dumps({"busy_threads": len(workers), "register_calls": len(register_calls), "restored": True}))
'''
    result = subprocess.run([sys.executable, "-c", source, str(tmp_path)],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"busy_threads": 50, "register_calls": 0, "restored": True}
    for index in range(50):
        assert f"Thread cache-signal-busy-{index:02} " in result.stderr
    assert "Gateway SIGUSR1 Python thread dump" in result.stderr
    assert "Segmentation fault" not in result.stderr


# receipt_fragment_merged
# --- W1 #53-4 回归片段；仅使用临时Gateway与真实JSON协议，不证明生产负载数字。 ---


def receipt_seed(paths, key='input-a', state='consumed', updated_at=1):
    prepared = {'id': key, 'kind': 'ask', 'goal': 'bounded input',
                'metadata': {'client_input_digest': 'digest-' + key}}
    row = receipt_delivery.GatewayInputReceipt(key, 'digest-' + key, key, state, prepared,
        receipt_delivery._prepared_payload_digest(prepared), key, updated_at=updated_at)
    receipt_write(receipt_delivery.gateway_input_receipt_path(paths, key), row.to_dict())
    return row


def receipt_outbox(paths, key, *, entries=None, updated_at=1):
    path = paths.root / 'client_outbox' / (key + '.json')
    receipt_write(path, {'schema_version': 'tui_active_input_outbox.v3',
                         'entries': {} if entries is None else entries, 'updated_at': updated_at})
    return path


def receipt_drain(paths, directory, **kwargs):
    results = []
    for _ in range(100):
        report = receipt_retention.archive_terminal_slice(paths, directory=directory, before=100, max_ms=1000, **kwargs)
        results.append(report)
        if report['complete']:
            return results
    pytest.fail('retention failed to make cross-slice progress')


def test_poison_receipt_quarantined_once(tmp_path, caplog):
    paths = receipt_paths(tmp_path / 'gateway')
    path = paths.root / 'input_receipts' / 'broken.json'
    path.parent.mkdir(parents=True)
    path.write_text('{broken')
    results = []
    for tick in range(10):
        summary = receipt_delivery.reconcile_gateway_input_receipts(paths, object())
        results.append(summary)
        if tick >= 3:
            receipt_delivery.raise_if_input_reconcile_unsettled(summary)
    assert results[0]['errors'] == results[1]['errors'] == 1
    assert all(row['quarantined'] == 1 and row['errors'] == 0 for row in results[3:])
    assert sum('GATEWAY_INPUT_RECEIPT_QUARANTINED' in row.message for row in caplog.records) == 1
    assert path.read_text() == '{broken'
    # 改正文而不改目录mtime；同名变代必须解除隔离，不能等目录重新扫描。
    directory_stamp = path.parent.stat().st_mtime_ns
    row = receipt_seed(paths, 'fixed')
    prepared = {**row.prepared_request, 'id': 'broken'}
    path.write_text(receipt_json.dumps(receipt_replace(row, request_id='broken',
        prepared_request=prepared, prepared_payload_digest=receipt_delivery._prepared_payload_digest(prepared)).to_dict()))
    receipt_os.utime(path.parent, ns=(directory_stamp, directory_stamp))
    recovered = receipt_delivery.reconcile_gateway_input_receipts(paths, object())
    assert recovered['errors'] == 0 and recovered['consumed'] == 2 and recovered['quarantined'] == 0


@pytest.mark.parametrize('error', [OSError('temporary'), BlockingIOError('busy'), PermissionError('temporarily denied')])
def test_receipt_transient_io_never_quarantines(tmp_path, monkeypatch, error):
    paths = receipt_paths(tmp_path / 'gateway')
    receipt_seed(paths)
    def receipt_fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(receipt_delivery, 'read_gateway_input_receipt', receipt_fail)
    for _ in range(10):
        summary = receipt_delivery.reconcile_gateway_input_receipts(paths, object())
        assert summary['errors'] == 1 and summary['quarantined'] == 0
        with pytest.raises(receipt_delivery.GatewayInputReconcileUnsettledError):
            receipt_delivery.raise_if_input_reconcile_unsettled(summary)


def test_receipt_failure_streak_resets_and_cache_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / 'one.json'
    path.write_text('{}')
    stamp = receipt_delivery._receipt_failure_stamp(path)
    cache = receipt_delivery._ReceiptFailureCache
    monkeypatch.setattr(cache, 'max_entries', 2)
    with cache.lock:
        cache.entries.clear()
    assert not receipt_delivery._record_receipt_failure(path, stamp, 'DataCorruptionError', 3)
    assert not receipt_delivery._record_receipt_failure(path, stamp, 'OSError', 3)
    assert not receipt_delivery._record_receipt_failure(path, stamp, 'DataCorruptionError', 3)
    assert not receipt_delivery._record_receipt_failure(path, stamp, 'DataCorruptionError', 3)
    assert receipt_delivery._record_receipt_failure(path, stamp, 'DataCorruptionError', 3)
    for index in range(4):
        other = tmp_path / f'{index}.json'
        other.write_text('{}')
        receipt_delivery._record_receipt_failure(other, receipt_delivery._receipt_failure_stamp(other), 'ValueError', 1)
    assert len(cache.entries) <= 2


def test_unchanged_dir_skips_scan_and_pending_retries(tmp_path, monkeypatch):
    paths = receipt_paths(tmp_path / 'gateway')
    receipt_seed(paths)
    calls, scan = [], receipt_delivery.os.scandir
    def receipt_scan(path):
        calls.append(str(path))
        return scan(path)
    monkeypatch.setattr(receipt_delivery.os, 'scandir', receipt_scan)
    receipt_delivery.reconcile_gateway_input_receipts(paths, object())
    calls.clear()
    summary = receipt_delivery.reconcile_gateway_input_receipts(paths, object())
    assert summary['checked'] == summary['consumed'] == 0
    assert not calls
    # 已消费整圈缓存不能掩蔽原地修改，也不能掩蔽pending的后续重试。
    path = receipt_delivery.gateway_input_receipt_path(paths, 'input-a')
    row = receipt_delivery.read_gateway_input_receipt(paths, 'input-a')
    payload = receipt_replace(row, state='active_pending').to_dict()
    directory_stamp = path.parent.stat().st_mtime_ns
    path.write_text(receipt_json.dumps(payload))
    assert path.parent.stat().st_mtime_ns == directory_stamp
    attempts = []
    def receipt_pending(*args):
        attempts.append(1)
        args[-1]['active_pending'] = args[-1].get('active_pending', 0) + 1
    monkeypatch.setattr(receipt_delivery, '_reconcile_unsettled_receipt', receipt_pending)
    receipt_delivery.reconcile_gateway_input_receipts(paths, object())
    receipt_delivery.reconcile_gateway_input_receipts(paths, object())
    assert attempts == [1, 1] and not calls


@pytest.mark.parametrize('directory', ['input_receipts', 'client_outbox'])
def test_retention_moves_only_terminal_old_entries(tmp_path, directory):
    paths = receipt_paths(tmp_path / 'gateway')
    if directory == 'input_receipts':
        receipt_seed(paths, 'old')
        receipt_seed(paths, 'young', updated_at=1000)
        receipt_seed(paths, 'pending', state='active_pending')
    else:
        receipt_outbox(paths, 'old')
        receipt_outbox(paths, 'young', updated_at=1000)
        receipt_outbox(paths, 'pending', entries={'message-a': {'request_id': 'pending'}})
    folder = paths.root / directory
    lock = folder / 'orphan.json.lock'
    lock.write_text('keep')
    reports = receipt_drain(paths, directory, max_entries=1)
    assert sum(row['archived_entries'] for row in reports) == 1
    assert all(row['scanned_entries'] <= 1 for row in reports)
    assert reports[0]['complete'] is False
    assert (paths.root / 'archive' / directory / '197001' / 'old.json').is_file()
    # SCALE-FU-01：读口已统一经封存索引回落，input与outbox一样在定位核实后移除松散原件。
    assert not (folder / 'old.json').exists()
    assert not any('reader_cutover_required' in row for row in reports)
    assert (folder / 'young.json').exists() and (folder / 'pending.json').exists()
    assert lock.read_text() == 'keep' and not any(row['errors'] for row in reports)
    if directory == 'input_receipts':
        row = receipt_delivery.read_gateway_input_receipt(paths, 'old')
        assert row.state == 'consumed'
        reused, created = receipt_delivery.load_or_prepare_gateway_input_locked(paths, request_id='old',
            client_input_digest=row.client_input_digest, client_message_id=row.client_message_id,
            guidance_dedupe_key=row.guidance_dedupe_key, prepared_request=row.prepared_request)
        assert not created and reused == row


def test_receipt_retention_byte_budget_active_file_and_symlink(tmp_path):
    paths = receipt_paths(tmp_path / 'gateway')
    receipt_seed(paths, 'old')
    receipt_seed(paths, 'active')
    receipt_write(paths.processing / 'active.json', {'id': 'active'})
    folder = paths.root / 'input_receipts'
    (folder / 'link.json').symlink_to(folder / 'old.json')
    report = receipt_retention.archive_terminal_slice(paths, directory='input_receipts', before=100,
        max_bytes=1, max_ms=1000)
    assert not report['complete'] and report['archived_entries'] == 0
    reports = receipt_drain(paths, 'input_receipts', max_entries=1)
    assert sum(row['archived_entries'] for row in reports) == 1
    assert (folder / 'active.json').exists() and (folder / 'link.json').is_symlink()
    with pytest.raises(ValueError, match='not_enabled'):
        receipt_retention.archive_terminal_slice(paths, directory='control_operations', before=100)


def test_receipt_retention_publish_failure_keeps_original(tmp_path, monkeypatch):
    paths = receipt_paths(tmp_path / 'gateway')
    row = receipt_seed(paths)
    def receipt_failed_write(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(receipt_retention, 'write_bytes_atomic_beneath', receipt_failed_write)
    reports = receipt_drain(paths, 'input_receipts')
    assert any(report['errors'] for report in reports)
    assert receipt_delivery.read_gateway_input_receipt(paths, row.request_id) == row
    assert receipt_delivery.gateway_input_receipt_path(paths, row.request_id).exists()


def test_receipt_scan_context_closed_and_lru_bounded(tmp_path, monkeypatch):
    # 缓存跨片拥有目录上下文；完成或逐出时必须关闭，不允许每拍新增一个未管理句柄。
    original, closed = receipt_retention.os.scandir, []
    class ReceiptScan:
        def __init__(self, path):
            self.inner = original(path)
        def __enter__(self):
            return self.inner
        def __exit__(self, *args):
            self.inner.close()
            closed.append(1)
    with receipt_retention._RuntimeScans.lock:
        for key in list(receipt_retention._RuntimeScans.entries):
            receipt_retention._close_scan(key)
    monkeypatch.setattr(receipt_retention.os, 'scandir', ReceiptScan)
    monkeypatch.setattr(receipt_retention._RuntimeScans, 'max_directories', 2)
    for index in range(4):
        paths = receipt_paths(tmp_path / str(index))
        receipt_outbox(paths, 'pending', entries={'a': {}})
        receipt_retention.archive_terminal_slice(paths, directory='client_outbox', before=100, max_entries=1, max_ms=1000)
    assert len(receipt_retention._RuntimeScans.entries) == 2 and len(closed) == 2
    for key in list(receipt_retention._RuntimeScans.entries):
        receipt_retention._close_scan(key)
    assert len(closed) == 4


def test_receipt_process_list_preserves_order_and_reports_budget(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import process_session_store as receipt_sessions
    from agent_py_agent.tests.test_process_session_store import _reservation as receipt_reservation
    store = receipt_sessions.ProcessSessionStore(tmp_path / 'sessions')
    for key in ('bg-z', 'bg-a', 'bg-m'):
        store.write(receipt_reservation(key))
    records, errors = store.list_records()
    assert [row['session_id'] for row in records] == ['bg-a', 'bg-m', 'bg-z'] and not errors
    calls, original = [], receipt_sessions.os.scandir
    class ReceiptScan:
        def __init__(self, path):
            self.inner = original(path)
        def __enter__(self):
            return self.inner
        def __exit__(self, *args):
            self.inner.close()
            calls.append('closed')
    monkeypatch.setattr(receipt_sessions.os, 'scandir', ReceiptScan)
    records, errors = store.list_records(max_entries=1)
    assert records == [] and len(errors) == 1 and 'process_session_scan_budget_exceeded' in errors[0]['message']
    assert calls == ['closed']
    records, errors = store.list_records(max_ms=0.000001)
    assert not records and errors


def test_receipt_maintenance_safe_gates_and_cross_slice_progress(tmp_path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home as receipt_home
    from agent_py_agent.agent.user_space.owner_maintenance import (
        run_owner_retention_if_due as receipt_maintain,
    )
    from agent_py_agent.agent.user_space.runtime_paths import (
        runtime_paths_for_agent as receipt_runtime_paths,
    )
    home = receipt_home(tmp_path / 'home')
    paths = receipt_paths(receipt_runtime_paths(None, home.owner_home_dir, home)['gateway_workspace'])
    receipt_seed(paths)
    original_input = receipt_delivery.gateway_input_receipt_path(paths, 'input-a').read_bytes()
    receipt_outbox(paths, 'empty')
    for tick in range(20):
        receipt_maintain(home, now=2000000 + tick, include_global_indexes=False,
                        quota_max_entries=4096, quota_max_ms=1000)
        state = receipt_json.loads((home.owner_data_dir / 'maintenance.json').read_text())
        if not state.get('maintenance_partial'):
            break
    assert not state['maintenance_partial']
    assert (paths.root / 'archive/input_receipts/197001/input-a.json').exists()
    assert (paths.root / 'archive/client_outbox/197001/empty.json').exists()
    assert not receipt_delivery.gateway_input_receipt_path(paths, 'input-a').exists()
    assert (paths.root / 'archive/input_receipts/197001/input-a.json').read_bytes() == original_input
    assert receipt_delivery.read_gateway_input_receipt(paths, 'input-a').to_dict() == receipt_json.loads(original_input)
    assert not (paths.root / 'client_outbox/empty.json').exists()


def test_receipt_retention_legal_hold_preserves_runtime_files(tmp_path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home as receipt_home
    from agent_py_agent.agent.user_space.owner_maintenance import (
        run_owner_retention_if_due as receipt_maintain,
    )
    from agent_py_agent.agent.user_space.runtime_paths import (
        runtime_paths_for_agent as receipt_runtime_paths,
    )
    home = receipt_home(tmp_path / 'home')
    paths = receipt_paths(receipt_runtime_paths(None, home.owner_home_dir, home)['gateway_workspace'])
    receipt_seed(paths)
    receipt_outbox(paths, 'empty')
    policy = receipt_json.loads(home.owner_retention_json.read_text())
    policy['legal_hold'] = True
    home.owner_retention_json.write_text(receipt_json.dumps(policy))
    receipt_maintain(home, now=2000000, include_global_indexes=False, quota_max_entries=4096, quota_max_ms=1000)
    assert receipt_delivery.gateway_input_receipt_path(paths, 'input-a').exists()
    assert (paths.root / 'client_outbox/empty.json').exists()
    assert not (paths.root / 'archive/runtime-files.sqlite3').exists()


def test_receipt_persisted_terminal_unknown_failure_is_quarantined(tmp_path, monkeypatch, caplog):
    paths = receipt_paths(tmp_path / 'gateway')
    receipt_seed(paths, state='terminal_unknown')
    def receipt_unsettled(paths, agent, path, summary):
        summary['checked'] += 1
        summary['terminal_unknown'] += 1
        summary['terminal_unknown_errors'] += 1
        summary['last_terminal_unknown_error'] = {'request_id': path.stem, 'error_type': 'DataCorruptionError'}
    monkeypatch.setattr(receipt_delivery, '_reconcile_receipt_path', receipt_unsettled)
    for tick in range(10):
        summary = receipt_delivery.reconcile_gateway_input_receipts(paths, object())
        if tick >= 3:
            receipt_delivery.raise_if_input_reconcile_unsettled(summary)
            assert summary['quarantined'] == 1
    assert sum('GATEWAY_INPUT_RECEIPT_QUARANTINED' in row.message for row in caplog.records) == 1


def test_receipt_json_report_io_failure_does_not_become_poison(tmp_path, monkeypatch):
    from types import SimpleNamespace as ReceiptNamespace
    paths = receipt_paths(tmp_path / 'gateway')
    receipt_seed(paths)
    original = receipt_delivery.read_json_file_report
    def receipt_io_report(path, **options):
        if path.name == 'input-a.json':
            return ReceiptNamespace(payload={}, lock_busy=False,
                load_error={'category': 'io', 'error_type': 'OSError'})
        return original(path, **options)
    monkeypatch.setattr(receipt_delivery, 'read_json_file_report', receipt_io_report)
    for _ in range(10):
        summary = receipt_delivery.reconcile_gateway_input_receipts(paths, object())
        assert summary['errors'] == 1 and summary['quarantined'] == 0


def test_receipt_plugin_owned_input_stays_for_unmigrated_reader(tmp_path):
    paths = receipt_paths(tmp_path / 'gateway')
    row = receipt_seed(paths)
    prepared = {**row.prepared_request, 'metadata': {
        **row.prepared_request['metadata'], 'plugin_provenance': {'installation_ref': 'test-installation'}}}
    row = receipt_replace(row, prepared_request=prepared,
        prepared_payload_digest=receipt_delivery._prepared_payload_digest(prepared))
    receipt_write(receipt_delivery.gateway_input_receipt_path(paths, row.request_id), row.to_dict())
    assert sum(item['archived_entries'] for item in receipt_drain(paths, 'input_receipts')) == 0
    assert receipt_delivery.gateway_input_receipt_path(paths, row.request_id).exists()

# retention_rework_fragment_merged
# --- 归档返工反例：丢索引失败关闭、未迁移读者保热件；供父级合并。 ---


def rework_seed(paths, key='input-a', *, state='consumed', metadata=None):
    prepared = {'id': key, 'kind': 'ask', 'goal': 'original complete request', 'user_id': 'user-a',
                'conversation': {'channel_conversation_id': 'conversation-a'},
                'metadata': {'client_input_digest': 'digest-' + key, 'channel': 'chat', **(metadata or {})}}
    row = rework_inputs.GatewayInputReceipt(key, 'digest-' + key, 'message-a', state, prepared,
        rework_inputs._prepared_payload_digest(prepared), 'original-guidance-key', updated_at=1)
    rework_write(rework_inputs.gateway_input_receipt_path(paths, key), row.to_dict())
    return row


def rework_outbox(paths, key, *, entries=None):
    path = paths.root / 'client_outbox' / (key + '.json')
    rework_write(path, {'schema_version': 'tui_active_input_outbox.v3',
                        'entries': {} if entries is None else entries, 'updated_at': 1})
    return path


def rework_archive(paths, directory):
    reports = []
    for _ in range(100):
        report = rework_retention.archive_terminal_slice(paths, directory=directory, before=100,
            max_entries=1, max_ms=1000)
        reports.append(report)
        if report['complete']:
            return reports
    pytest.fail('bounded retention failed to finish')


def rework_prepare(paths, row, prepare=None):
    return rework_inputs.load_or_prepare_gateway_input_locked(paths, request_id=row.request_id,
        client_input_digest=row.client_input_digest, client_message_id=row.client_message_id,
        guidance_dedupe_key=row.guidance_dedupe_key, prepared_request=row.prepared_request,
        prepare_request=prepare)


@pytest.mark.parametrize('legacy_without_marker', [False, True])
def test_rework_missing_index_never_reprepares_consumed_input(tmp_path, legacy_without_marker):
    paths = rework_paths(tmp_path / 'gateway')
    row = rework_seed(paths)
    assert not any(r['errors'] for r in rework_archive(paths, 'input_receipts'))
    cold = paths.root / 'archive/input_receipts/197001/input-a.json'
    assert json.loads(cold.read_text()) == row.to_dict()
    # 模拟已由旧版本删掉热件的存量；不是让新生产路径执行危险删除。
    hot = rework_inputs.gateway_input_receipt_path(paths, row.request_id)
    hot.unlink(missing_ok=True)
    marker = paths.root / 'archive/runtime-files-published.json'
    if legacy_without_marker:
        marker.unlink(missing_ok=True)
    (paths.root / 'archive/runtime-files.sqlite3').unlink()
    prepare = Mock(side_effect=AssertionError('must not prepare a second input'))
    reused, created = rework_prepare(paths, row, prepare)
    assert reused == row and not created
    prepare.assert_not_called()
    assert not hot.exists() and not (paths.root / 'archive/runtime-files.sqlite3').exists()
    assert json.loads(cold.read_text()) == row.to_dict()


def test_rework_writer_rebuilds_lost_index_from_cold_copies(tmp_path, monkeypatch):
    paths = rework_paths(tmp_path / 'gateway')
    rework_outbox(paths, 'first')
    assert not any(r['errors'] for r in rework_archive(paths, 'client_outbox'))
    index = paths.root / 'archive/runtime-files.sqlite3'
    index.unlink()
    def publish(source, destination):
        destination = Path(destination)
        if destination.exists():
            raise FileExistsError(destination)
        destination.write_bytes(Path(source).read_bytes())
    monkeypatch.setattr(rework_retention.os, 'link', publish)
    second = rework_outbox(paths, 'second')
    before = second.read_bytes()
    reports = rework_archive(paths, 'client_outbox')
    assert not any(r['errors'] for r in reports)
    assert rework_retention.lookup_runtime_file(paths, 'client_outbox', 'first.json') == (
        paths.root / 'archive/client_outbox/197001/first.json')
    assert not second.exists() and (paths.root / 'archive/client_outbox/197001/second.json').read_bytes() == before


def test_rework_input_copy_retains_original_and_original_prepare_identity(tmp_path):
    paths = rework_paths(tmp_path / 'gateway')
    row = rework_seed(paths)
    hot = rework_inputs.gateway_input_receipt_path(paths, row.request_id)
    before = hot.read_bytes()
    reports = rework_archive(paths, 'input_receipts')
    # SCALE-FU-01：原件在封存定位核实后移除，同id重试仍经封存件保持原prepare身份。
    assert not hot.exists()
    assert (paths.root / 'archive/input_receipts/197001/input-a.json').read_bytes() == before
    assert not any(r['errors'] for r in reports)
    reused, created = rework_prepare(paths, row)
    assert not created and reused == row
    with pytest.raises(ValueError):
        rework_inputs.load_or_prepare_gateway_input_locked(paths, request_id=row.request_id,
            client_input_digest='another input', client_message_id=row.client_message_id,
            guidance_dedupe_key=row.guidance_dedupe_key, prepared_request=row.prepared_request)


def test_rework_late_control_still_observes_hot_receipt_without_ordinary_metadata(tmp_path):
    from agent_py_agent.agent.gateway_parts import control_service as controls
    from agent_py_agent.agent.gateway_parts.http_handlers import http_input_identity

    paths = rework_paths(tmp_path / 'gateway')
    scope = controls.GatewayControlScope('user-a', 'chat', 'conversation-a', metadata={'message_id': 'message-a'})
    key, _ = http_input_identity(user_id=scope.user_id, channel=scope.channel,
        conversation_id=scope.conversation_id, message_id='message-a')
    rework_seed(paths, key)
    store = SimpleNamespace(guidance=SimpleNamespace(append_once=Mock()))
    before = controls._append_ordinary_input_guidance(store, paths, scope, {'message': 'supplement'}, 'turn-a', None)
    rework_archive(paths, 'input_receipts')
    after = controls._append_ordinary_input_guidance(store, paths, scope, {'message': 'supplement'}, 'turn-a', None)
    store.guidance.append_once.assert_not_called()
    assert before.error_code == after.error_code == 'INPUT_IDENTITY_CONFLICT'


def test_rework_production_maintenance_retains_input_but_archives_empty_outbox(tmp_path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due
    from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent

    home = ensure_my_agent_home(tmp_path / 'home')
    paths = rework_paths(runtime_paths_for_agent(None, home.owner_home_dir, home)['gateway_workspace'])
    row = rework_seed(paths)
    hot = rework_inputs.gateway_input_receipt_path(paths, row.request_id)
    original = hot.read_bytes()
    outbox = rework_outbox(paths, 'empty')
    pending = rework_outbox(paths, 'pending', entries={'message': {'request_id': 'pending'}})
    for tick in range(20):
        run_owner_retention_if_due(home, now=2000000 + tick, include_global_indexes=False,
            quota_max_entries=4096, quota_max_ms=1000)
        state = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
        if not state.get('maintenance_partial'):
            break
    assert not state['maintenance_partial']
    # SCALE-FU-01：control/wait读口已回落封存件，生产维护移除已核实的input原件。
    assert not hot.exists() and pending.exists() and not outbox.exists()
    assert (paths.root / 'archive/input_receipts/197001/input-a.json').read_bytes() == original
    assert (paths.root / 'archive/client_outbox/197001/empty.json').exists()


def test_rework_cold_read_disappearing_between_lookup_and_read_is_not_fresh(tmp_path, monkeypatch):
    paths = rework_paths(tmp_path / 'gateway')
    row = rework_seed(paths)
    rework_archive(paths, 'input_receipts')
    hot = rework_inputs.gateway_input_receipt_path(paths, row.request_id)
    hot.unlink(missing_ok=True)
    original_read = rework_inputs.read_json_file_report

    def vanished(path, **kwargs):
        if 'archive' in path.parts:
            return SimpleNamespace(payload={}, load_error=None, lock_busy=False)
        return original_read(path, **kwargs)

    monkeypatch.setattr(rework_inputs, 'read_json_file_report', vanished)
    with pytest.raises(ReworkCorruption):
        rework_prepare(paths, row)
    assert not hot.exists()


def test_rework_publication_marker_alone_prevents_missing_index_becoming_absence(tmp_path):
    paths = rework_paths(tmp_path / 'gateway')
    rework_outbox(paths, 'old')
    rework_archive(paths, 'client_outbox')
    marker = paths.root / 'archive/runtime-files-published.json'
    assert json.loads(marker.read_text())['schema_version'] == 'gateway_runtime_files_published.v1'
    cold = paths.root / 'archive/client_outbox/197001/old.json'
    cold.unlink()
    cold.parent.rmdir()
    cold.parent.parent.rmdir()
    (paths.root / 'archive/runtime-files.sqlite3').unlink()
    with pytest.raises(ReworkCorruption):
        rework_retention.lookup_runtime_file(paths, 'input_receipts', 'unseen.json')


def test_rework_genuinely_new_gateway_can_prepare(tmp_path):
    paths = rework_paths(tmp_path / 'gateway')
    assert rework_retention.lookup_runtime_file(paths, 'input_receipts', 'first.json') is None
    row = rework_seed(paths, 'first')
    rework_inputs.gateway_input_receipt_path(paths, 'first').unlink()
    created_row, created = rework_prepare(paths, row)
    assert created and created_row.state == 'pending'
    assert created_row.prepared_request == row.prepared_request


@pytest.mark.parametrize('stage', ['marker', 'bytes', 'index'])
def test_rework_publish_failure_never_deletes_source(tmp_path, monkeypatch, stage):
    paths = rework_paths(tmp_path / 'gateway')
    path = rework_outbox(paths, 'old')
    original = path.read_bytes()
    write = rework_retention.write_bytes_atomic_beneath
    index = rework_retention._index

    def broken_write(root, parts, raw):
        if (stage == 'marker' and parts[-1] == 'runtime-files-published.json'
                or stage == 'bytes' and parts[-1] == 'old.json'):
            raise OSError('injected publication failure')
        return write(root, parts, raw)

    def broken_index(paths, *, writable=False):
        if stage == 'index' and writable:
            raise OSError('injected index failure')
        return index(paths, writable=writable)

    monkeypatch.setattr(rework_retention, 'write_bytes_atomic_beneath', broken_write)
    monkeypatch.setattr(rework_retention, '_index', broken_index)
    reports = rework_archive(paths, 'client_outbox')
    assert any(r['errors'] for r in reports) and path.read_bytes() == original


def test_rework_repeated_input_copy_is_idempotent_and_budgeted(tmp_path):
    paths = rework_paths(tmp_path / 'gateway')
    row = rework_seed(paths)
    hot = rework_inputs.gateway_input_receipt_path(paths, row.request_id)
    report = rework_retention.archive_terminal_slice(paths, directory='input_receipts', before=100,
        max_bytes=1, max_ms=1000)
    assert not report['complete'] and report['bytes'] == 0 and hot.exists()
    before = hot.read_bytes()
    first = rework_archive(paths, 'input_receipts')
    assert sum(r['archived_entries'] for r in first) == 1
    again = rework_archive(paths, 'input_receipts')
    assert sum(r['archived_entries'] for r in again) == 0
    assert all(r['scanned_entries'] <= 1 for r in again)
    assert not hot.exists()
    assert (paths.root / 'archive/input_receipts/197001/input-a.json').read_bytes() == before


# === test_v13_w5_bounded_walk.py: W5：预算约束的是实际目录访问，不只是最终返回数。 ===

def _observer(monkeypatch):
    original = os.scandir
    visited = []

    def observe(path):
        visited.append(Path(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", observe)
    return visited


def _walk():
    from agent_py_agent.agent.common import bounded_walk as module
    return module


def test_walk_prunes_before_entering_and_preserves_open_world(tmp_path, monkeypatch):
    for name in (".git", "__pycache__", "Library", "node_modules", "ordinary"):
        child = tmp_path / name
        child.mkdir()
        (child / "report.json").write_text("{}")
    visited = _observer(monkeypatch)
    module = _walk()
    result = module.bounded_walk(tmp_path, include=lambda entry: entry.name.endswith(".json"))
    assert {path.parent.name for path in result.paths} == {"Library", "node_modules", "ordinary"}
    assert not any(path.name in {".git", "__pycache__"} for path in visited)
    assert result.truncated is False
    assert result.coverage["stop_reason"] == "complete"
    assert result.coverage["visited_dirs"] == len(visited)
    assert result.coverage["pruned_dirs"] == 2


@pytest.mark.parametrize("budget,reason", [
    ({"max_entries": 1}, "max_entries"),
    ({"max_dirs": 1}, "max_dirs"),
    ({"max_depth": 0}, "max_depth"),
    ({"deadline_seconds": 0}, "deadline"),
])
def test_walk_reports_each_budget(tmp_path, budget, reason):
    (tmp_path / "child").mkdir()
    (tmp_path / "child" / "report.json").write_text("{}")
    result = _walk().bounded_walk(tmp_path, include=lambda entry: entry.name.endswith(".json"), **budget)
    assert result.truncated is True
    assert result.coverage["stop_reason"] == reason
    assert result.coverage["elapsed_ms"] >= 0
    if "max_entries" in budget:
        assert result.coverage["visited_entries"] <= budget["max_entries"]
    if "max_dirs" in budget:
        assert result.coverage["visited_dirs"] <= budget["max_dirs"]


def test_walk_does_not_follow_directory_symlinks(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.json").write_text("{}")
    root = tmp_path / "root"
    root.mkdir()
    (root / "alias").symlink_to(outside, target_is_directory=True)
    visited = _observer(monkeypatch)
    result = _walk().bounded_walk(root, include=lambda entry: entry.name.endswith(".json"))
    assert result.paths == ()
    assert visited == [root]


@pytest.mark.parametrize("noise,old_calls", [(10, 30), (100, 210), (500, 1010)])
@pytest.mark.parametrize("max_files", [0, 1])
def test_json_paths_scandir_reproduction(tmp_path, monkeypatch, noise, old_calls, max_files):
    for number in range(noise):
        parent = tmp_path / ("ordinary", ".git", "__pycache__")[number % 3] / str(number)
        parent.mkdir(parents=True)
        (parent / "noise.json").write_text("{}")
    (tmp_path / "first.json").write_text("{}")
    (tmp_path / "second.json").write_text("{}")
    visited = _observer(monkeypatch)
    result = contract_status._json_paths(tmp_path, max_files=max_files)
    assert len(result) == max_files
    assert len(visited) < old_calls
    assert not any(".git" in path.parts or "__pycache__" in path.parts for path in visited)


def test_status_and_review_propagate_incomplete_candidates(tmp_path, monkeypatch):
    (tmp_path / "report.json").write_text('{"ok": true}')
    module = _walk()
    partial = module.BoundedWalkResult(
        paths=(tmp_path / "report.json",), truncated=True,
        coverage={"stop_reason": "max_entries", "visited_entries": 1, "visited_dirs": 1,
                  "pruned_dirs": 0, "elapsed_ms": 0.0},
    )
    monkeypatch.setattr(contract_status, "bounded_walk", lambda *args, **kwargs: partial)
    monkeypatch.setattr(real_run_review, "bounded_walk", lambda *args, **kwargs: partial)
    status = contract_status.summarize_contract_status(tmp_path).to_dict()
    review = real_run_review.review_real_run_directory(tmp_path).to_dict()
    assert status["incomplete"] is True
    assert status["coverage"]["stop_reason"] == "max_entries"
    assert review["candidate_coverage_incomplete"] is True
    assert review["final_status"] == "UNKNOWN"


def test_locator_preserves_early_result_limit(tmp_path, monkeypatch):
    (tmp_path / "hit.txt").write_text("yes")
    for number in range(30):
        (tmp_path / str(number)).mkdir()
    visited = _observer(monkeypatch)
    result = artifact_locator._candidate_paths_under_root(tmp_path, (".txt",), remaining=1)
    assert result == [(tmp_path / "hit.txt").resolve()]
    assert len(visited) == 1


def test_locator_no_match_is_bounded(tmp_path, monkeypatch):
    module = _walk()
    original = module.bounded_walk
    observed = []

    def small_budget(root, **kwargs):
        result = original(root, max_entries=3, **kwargs)
        observed.append(result)
        return result

    for number in range(8):
        (tmp_path / str(number)).mkdir()
    monkeypatch.setattr(artifact_locator, "bounded_walk", small_budget)
    result = artifact_locator.locate_artifact({"kind": "txt", "search_roots": [str(tmp_path)]}, tmp_path)
    assert result.path is None
    assert result.findings[0]["code"] == "ARTIFACT_LOCATOR_INCOMPLETE"
    assert observed[0].coverage["visited_entries"] <= 3


def test_suffix_first_match_is_not_unique_after_truncation(tmp_path, monkeypatch):
    module = _walk()
    found = tmp_path / "result.txt"
    found.write_text("x")
    partial = module.BoundedWalkResult((found,), True, {"stop_reason": "max_dirs"})
    monkeypatch.setattr(result_artifact_evidence, "bounded_walk", lambda *args, **kwargs: partial)
    assert result_artifact_evidence._resolve_by_suffix(Path("result.txt"), [tmp_path]) is None


def test_suffix_unique_match_when_coverage_complete(tmp_path):
    found = tmp_path / "nested" / "result.txt"
    found.parent.mkdir()
    found.write_text("x")
    assert result_artifact_evidence._resolve_by_suffix(Path("result.txt"), [tmp_path]) == found


# === test_v13_w5_path_policy_workspace.py: 显式工作区只豁免危险根，不扩大 owner、凭据或宿主保护边界；全用任务临时树。 ===

@pytest.fixture
def layout(tmp_path, monkeypatch):
    """分离模拟系统危险根与宿主数据根，避免旧数据根豁免掩盖回归。"""
    home = tmp_path / "home"
    data = tmp_path / "agent-data"
    root = tmp_path / "root"
    workspace = root / ".local/share/x/projects/ws"
    workspace.mkdir(parents=True)
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_AGENT_HOME", str(data))
    monkeypatch.setattr(PathAccessPolicy, "_workspace_warning_emitted", False, raising=False)
    return SimpleNamespace(home=home, data=data, root=root, workspace=workspace, base=tmp_path)


def _config(layout, workspace_root=None, **kwargs):
    """通过生产 from_config 入口传结构化配置，不替换真实路径裁决。"""
    return SimpleNamespace(
        path_access_mode="normal",
        path_dangerous_roots=[str(layout.root)],
        workspace_root=str(layout.workspace) if workspace_root is None else workspace_root,
        **kwargs,
    )


@pytest.mark.parametrize("as_list", [False, True])
def test_config_workspace_str_or_list_allows_strict_descendant(layout, as_list):
    value = [str(layout.workspace)] if as_list else str(layout.workspace)
    policy = PathAccessPolicy.from_config(_config(layout, value), agent_home_root=layout.data)
    decision = policy.check_write(layout.workspace / "output/report.txt")
    assert decision.allowed, decision
    assert layout.workspace.resolve() in policy.workspace_roots
    assert not policy.check(layout.workspace).allowed
    assert policy.check(layout.workspace.parent / "sibling/report.txt").code == "PATH_DANGEROUS_ROOT_BLOCKED"
    assert policy.check(layout.workspace.with_name("ws-prefix") / "report.txt").code == "PATH_DANGEROUS_ROOT_BLOCKED"


def test_from_values_accepts_frozen_keyword_workspace_roots(layout):
    values = [str(layout.workspace), str(layout.workspace), "", "bad\x00root"]
    policy = PathAccessPolicy.from_values(
        dangerous_roots=[layout.root], agent_home_root=layout.data, workspace_roots=values,
    )
    values.clear()
    assert policy.workspace_roots == (layout.workspace.resolve(),)
    assert policy.check(layout.workspace / "output.txt").allowed


def test_config_list_freezes_normalized_roots_and_ignores_empty_invalid(layout, monkeypatch):
    monkeypatch.setenv("W5_WORKSPACE", str(layout.workspace))
    values = ["$W5_WORKSPACE", "", "bad\x00root", str(layout.workspace / ".." / "ws")]
    cfg = _config(layout, values)
    policy = PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    values.clear()
    cfg.workspace_root = str(layout.root)
    assert policy.check(layout.workspace / "report.txt").allowed
    assert policy.workspace_roots.count(layout.workspace.resolve()) == 1


@pytest.mark.parametrize("kind", ["equal", "ancestor", "contains_nested"])
def test_workspace_equal_to_or_containing_dangerous_root_never_exempts(layout, kind):
    workspace = layout.root if kind == "equal" else layout.base
    cfg = _config(layout, str(workspace))
    if kind == "contains_nested":
        workspace = layout.workspace
        cfg.workspace_root = str(workspace)
        cfg.path_dangerous_roots.append(str(workspace / "nested-sensitive"))
    policy = PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    target = layout.workspace / "ordinary.txt"
    assert policy.check(target).code == "PATH_DANGEROUS_ROOT_BLOCKED"


def test_workspace_symlink_and_target_escape_use_realpath(layout):
    etc = layout.base / "etc"
    etc.mkdir()
    alias = layout.base / "workspace-alias"
    alias.symlink_to(layout.workspace, target_is_directory=True)
    (layout.workspace / "escape").symlink_to(etc, target_is_directory=True)
    (layout.workspace / "sibling").symlink_to(layout.root / "private", target_is_directory=True)
    cfg = _config(layout, str(alias))
    cfg.path_dangerous_roots.append(str(etc))
    policy = PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    assert policy.check(alias / "ok.txt").allowed
    assert policy.check(alias / "escape/shadow").code == "PATH_DANGEROUS_ROOT_BLOCKED"
    assert policy.check(alias / "sibling/key").code == "PATH_DANGEROUS_ROOT_BLOCKED"
    assert policy.check(alias / ".." / "outside.txt").code == "PATH_DANGEROUS_ROOT_BLOCKED"


def test_workspace_alias_of_dangerous_root_does_not_exempt(layout):
    alias = layout.base / "root-alias"
    alias.symlink_to(layout.root, target_is_directory=True)
    policy = PathAccessPolicy.from_config(_config(layout, str(alias)), agent_home_root=layout.data)
    assert policy.check(alias / "secret").code == "PATH_DANGEROUS_ROOT_BLOCKED"


@pytest.mark.parametrize("workspace_root", ["", [], ["", "bad\x00root"]])
def test_no_explicit_workspace_preserves_legacy_denial(layout, workspace_root):
    policy = PathAccessPolicy.from_config(_config(layout, workspace_root), agent_home_root=layout.data)
    assert policy.check(layout.workspace / "report.txt").code == "PATH_DANGEROUS_ROOT_BLOCKED"


def test_owner_workspace_derived_from_trusted_owner_or_local_main(layout):
    owner = layout.data / "owners/providers/test/users/alice"
    scoped = PathAccessPolicy.from_config(_config(layout, ""), owner_scope_root=owner, agent_home_root=layout.data)
    local = PathAccessPolicy.from_config(_config(layout, ""), agent_home_root=layout.data)
    assert owner / "workspace" in scoped.workspace_roots
    assert layout.data / "owners/local/main/workspace" in local.workspace_roots


def test_workspace_does_not_bypass_credential_filenames_or_host_exclusive(layout):
    source = layout.workspace / "host-config"
    cfg = _config(layout, host_exclusive_path_roots=[str(source)])
    policy = PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    assert policy.check(layout.workspace / ".env").code == "PATH_CREDENTIAL_FILE_BLOCKED"
    assert policy.check(layout.workspace / "auth.json").code == "PATH_CREDENTIAL_FILE_BLOCKED"
    assert policy.check(layout.workspace / ".env.example").allowed
    assert policy.check(source / "config.txt").code == "PATH_HOST_CREDENTIAL_BLOCKED"


def test_workspace_does_not_bypass_host_credential_or_managed_and_write_guards(layout):
    owner = layout.data / "owners/local/main"
    cfg = _config(layout, [str(layout.data / "config"), str(owner)])
    policy = PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    assert policy.check(layout.data / "config/shared-model-profiles.json").code == "PATH_HOST_CREDENTIAL_BLOCKED"
    assert policy.check(owner / "data/plugins/payload.txt").code == "PATH_HOST_MANAGED_STORE_BLOCKED"
    assert policy.check_write(owner / "config/options.json").code == "PATH_HOST_CONFIG_WRITE_BLOCKED"
    assert policy.check_write(owner / "workspace/runtime/state.json").code == "PATH_HOST_STATE_WRITE_BLOCKED"


@pytest.mark.parametrize("mode", ["normal", "full"])
def test_explicit_workspace_never_bypasses_owner_wall(layout, mode):
    owner = layout.data / "owners/providers/test/users/alice"
    other = layout.data / "owners/providers/test/users/bob"
    cfg = _config(layout, [str(layout.workspace), str(other)])
    cfg.path_access_mode = mode
    policy = PathAccessPolicy.from_config(cfg, agent_home_root=layout.data, owner_scope_root=owner)
    assert policy.check(layout.workspace / "report.txt").code == "PATH_OWNER_SCOPE_BLOCKED"
    assert policy.check(other / "workspace/report.txt").code == "PATH_CROSS_OWNER_BLOCKED"
    assert policy.check(owner / "workspace/report.txt").allowed


def test_startup_warning_once_per_process_across_policy_instances(layout, caplog):
    caplog.set_level(logging.WARNING)
    cfg = _config(layout)
    PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    PathAccessPolicy.from_config(cfg, agent_home_root=layout.data)
    records = [r for r in caplog.records if "PATH_WORKSPACE_UNDER_DANGEROUS_ROOT" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert str(layout.workspace) in records[0].getMessage()
    assert str(layout.root) in records[0].getMessage()


def test_safe_config_does_not_consume_warning_and_concurrent_builds_warn_once(layout, caplog):
    caplog.set_level(logging.WARNING)
    PathAccessPolicy.from_config(_config(layout, str(layout.base / "safe")), agent_home_root=layout.data)
    assert not [r for r in caplog.records if "PATH_WORKSPACE_UNDER_DANGEROUS_ROOT" in r.getMessage()]
    cfg = _config(layout)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: PathAccessPolicy.from_config(cfg, agent_home_root=layout.data), range(12)))
    records = [r for r in caplog.records if "PATH_WORKSPACE_UNDER_DANGEROUS_ROOT" in r.getMessage()]
    assert len(records) == 1


def test_file_tool_external_grant_keeps_workspace_and_hard_guards(layout):
    from agent_py_agent.agent.tooling._filesystem_read import (
        ReadFileTool,
        filesystem_access_options,
    )

    owner = layout.data / "owners/providers/test/users/alice"
    protected = layout.workspace / "host-config"
    policy = PathAccessPolicy.from_config(
        _config(layout, host_exclusive_path_roots=[str(protected)]),
        agent_home_root=layout.data, owner_scope_root=owner,
    )
    target = layout.workspace / "report.txt"
    target.write_text("workspace report", encoding="utf-8")
    tool = ReadFileTool(owner, 1000, [owner], filesystem_access_options(owner_scope_root=str(owner)))
    tool.path_access_policy = policy
    assert tool.check_path_access(target).code == "PATH_OWNER_SCOPE_BLOCKED"
    tool.granted_external_roots = (layout.workspace.resolve(),)
    assert tool.resolve_path(target) == target.resolve()
    assert tool.execute({"path": str(target)}).ok is True
    assert tool.check_path_access(layout.workspace / ".env").code == "PATH_CREDENTIAL_FILE_BLOCKED"
    assert tool.check_path_access(protected / "options.txt").code == "PATH_HOST_CREDENTIAL_BLOCKED"


# === test_v13_w5_runner_stop_escalation.py: #54 独占 runner 停止合同；PID、信号与时钟全为替身，不操作真实宿主。 ===

@pytest.fixture
def runner(monkeypatch):
    """冻结假身份，记录核验/信号/等待，禁止探测或触碰真实 PID。"""
    state = SimpleNamespace(pid=99999, birth="birth", alive=True, clock=0.0, events=[], on_sleep=None, on_kill=None)

    def alive(pid):
        assert pid == state.pid
        return state.alive

    def capture(pid):
        assert pid == state.pid
        state.events.append(("birth", state.birth))
        return state.birth

    def matches(pid, expected, current):
        assert pid == state.pid and expected == "birth"
        state.events.append(("verify", current))
        return current == expected

    def kill(pid, signum):
        assert pid == state.pid
        # 最后一个事件必须是本次 birth 核验，不可复用宽限期前的证明。
        assert state.events[-1] == ("verify", "birth")
        state.events.append(("signal", signum, state.clock))
        if state.on_kill:
            state.on_kill(signum)

    def sleep(seconds):
        assert seconds > 0
        state.clock += seconds
        state.events.append(("sleep", seconds))
        if state.on_sleep:
            state.on_sleep()

    state.sleep = sleep
    state.now = lambda: state.clock
    monkeypatch.setattr(hosts, "is_pid_alive", alive)
    monkeypatch.setattr(hosts, "capture_process_birth_token", capture)
    monkeypatch.setattr(hosts, "_birth_token_matches", matches)
    monkeypatch.setattr("os.kill", kill)
    return state


def _stop(*, status="cooperative_requested", pid=99999, birth="birth"):
    return hosts.FrozenRunnerStop({"status": status, "pid": pid}, pid, birth)


def _escalate(runner, stop=None, **kwargs):
    return hosts.escalate_exclusive_runner_stop(stop or _stop(), sleep=runner.sleep, now=runner.now, **kwargs)


def test_running_worker_gets_original_relay_tick_before_host_cleanup(runner):
    runner.on_sleep = lambda: setattr(runner, 'alive', False) if runner.clock >= 5.0 else None
    stop = hosts.FrozenRunnerStop({'status': 'cooperative_requested', 'pid': runner.pid},
                                 runner.pid, 'birth', 5.0, True)
    result = _escalate(runner, stop)
    assert result['host_exited'] is True
    assert result['signals'] == []
    assert 5.0 <= runner.clock <= 5.5


def test_unconfirmed_foreground_cleanup_never_orphans_process_group(runner):
    stop = hosts.FrozenRunnerStop({'status': 'cooperative_requested', 'pid': runner.pid},
                                 runner.pid, 'birth', 1000.0, True)
    result = _escalate(runner, stop)
    assert result['host_exited'] is False
    assert result['cleanup_state'] == 'unconfirmed'
    assert result['reason_code'] == 'RUNNER_TOOL_EXIT_UNCONFIRMED'
    assert result['signals'] == []
    assert runner.clock <= 10.1


def test_freeze_preserves_relay_interval_and_does_not_authorize_orphaning(monkeypatch):
    session = {'in_process': False, 'worker_pid': 1234, 'worker_birth_token': 'original',
               'launch_id': 'launch-1', 'attempt_id': 'attempt-1', 'interval_seconds': 5.0}
    task = SimpleNamespace(id='run-1', attributes={'background_start': {
        'launch_id': 'launch-1', 'attempt_id': 'attempt-1', 'pid': 1234}})
    monkeypatch.setattr(hosts, 'has_fresh_runner_session', lambda _: True)
    monkeypatch.setattr(hosts, 'runner_session_of', lambda _: session)
    stop = hosts.freeze_runner_stop(task, [task], {'run-1'}, kill_process=True)
    assert stop.pid == 1234 and stop.birth_token == 'original'
    assert stop.relay_interval_seconds == 5.0
    assert stop.cleanup_unconfirmed is True


def test_stale_cancelled_runner_retains_original_identity_for_recheck(monkeypatch):
    session = {'in_process': False, 'worker_pid': 1234, 'worker_birth_token': 'original',
               'launch_id': 'launch-1', 'attempt_id': 'attempt-1', 'interval_seconds': 5.0,
               'run_id': 'run-1', 'session_id': 'session-1', 'status': 'running'}
    task = SimpleNamespace(id='run-1', runner_active_attempt_id='', attributes={
        'background_start': {'launch_id': 'launch-1', 'attempt_id': 'attempt-1', 'pid': 1234},
        'runner_session': session,
        'cancel_subagents': {'stop_receipt': {'schema': 'runner-stop.v1',
                                             'run_id': 'run-1', 'attempt_id': 'attempt-1'}}})
    monkeypatch.setattr(hosts, 'has_fresh_runner_session', lambda _: False)
    stop = hosts.freeze_runner_stop(task, [task], {'run-1'}, kill_process=True)
    assert stop.pid == 1234 and stop.birth_token == 'original'
    assert stop.cleanup_unconfirmed is True
    task.runner_active_attempt_id = 'attempt-next'
    assert hosts.freeze_runner_stop(task, [task], {'run-1'}, kill_process=True).report['status'] == 'no_pid'


def test_worker_exit_without_tool_receipt_is_not_process_cleanup(runner):
    runner.alive = False
    stop = hosts.FrozenRunnerStop({'status': 'cooperative_requested', 'pid': runner.pid},
                                 runner.pid, 'birth', 5.0, True)
    result = cancellation.cleanup_subagent_stops(_batch(stop))
    report = result['cancelled'][0]['pid_report']
    assert report['host_exited'] is True and report['tools_exit_confirmed'] is False
    assert result['ok'] is False
    assert result['failed'][0]['error_code'] == 'RUNNER_TOOL_EXIT_UNCONFIRMED'
    assert not any(event[0] == 'signal' for event in runner.events)


def test_original_worker_final_receipt_confirms_cleanup_but_reused_birth_does_not(tmp_path, runner):
    import json

    runner.alive = False
    path = tmp_path / 'canonical_state.json'
    payload = {'id': 'run-123', 'runner_active_attempt_id': '', 'attributes': {
        'cancel_subagents': {'stop_receipt': {'schema': 'runner-stop.v1', 'run_id': 'run-123', 'attempt_id': 'a1'}},
        'runner_session': {'run_id': 'run-123', 'attempt_id': 'a1', 'session_id': 's1',
                           'worker_pid': runner.pid, 'worker_birth_token': 'birth',
                           'status': 'failed', 'ended_at': 10.0}}}
    path.write_text(json.dumps(payload))
    stop = hosts.FrozenRunnerStop({'status': 'cooperative_requested', 'pid': runner.pid},
                                 runner.pid, 'birth', 5.0, True, str(path), 'run-123', 'a1', 's1')
    assert cancellation.cleanup_subagent_stops(_batch(stop))['ok'] is True
    payload['attributes']['runner_session']['worker_birth_token'] = 'reused'
    path.write_text(json.dumps(payload))
    assert cancellation.cleanup_subagent_stops(_batch(stop))['ok'] is False
    path.write_text('{broken')
    result = cancellation.cleanup_subagent_stops(_batch(stop))
    proof = result['cancelled'][0]['pid_report']
    assert proof['proof_error_type'] == 'JSONDecodeError'
    assert proof['proof_reason'] == 'ORIGINAL_SESSION_EXIT_PROOF_UNREADABLE'
    assert proof['proof_next_action']


def test_abnormal_real_worker_exit_keeps_live_command_unconfirmed():
    import sys
    import time

    code = ("import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(2)'], "
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True); "
            "print(p.pid,flush=True); time.sleep(.3)")
    with subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True) as worker:
        command_pid = int(worker.stdout.readline())
        birth = hosts.capture_process_birth_token(worker.pid)
        assert birth
        worker.wait(timeout=3)
        stop = hosts.FrozenRunnerStop({'status': 'cooperative_requested', 'pid': worker.pid},
                                     worker.pid, birth, 5.0, True)
        result = cancellation.cleanup_subagent_stops(_batch(stop))
        assert hosts.is_pid_alive(command_pid)
        assert result['ok'] is False
        assert result['cancelled'][0]['pid_report']['host_exited'] is True
        assert result['cancelled'][0]['pid_report']['tools_exit_confirmed'] is False
        # 不给无退出证明的命令发信号；该测试命令自行结束，不留下活跃孤儿。
        deadline = time.monotonic() + 4
        while hosts.is_pid_alive(command_pid) and time.monotonic() < deadline:
            time.sleep(.05)
        assert not hosts.is_pid_alive(command_pid)


def test_idle_handler_proof_uses_exact_attempt_without_requiring_logs(tmp_path):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / 'runtime.db')
    first = repo.record_run_creation(owner_id='local/main', run_id='first', goal='执行')
    other = repo.record_run_creation(owner_id='local/main', run_id='other', goal='独立空闲轮')
    manager = SimpleNamespace(runtime_db=repo)
    assert cancellation._runner_never_started_handler(manager, first)
    op = repo.create_tool_operation(agent_run_id=first['agent_run_id'], attempt_id=first['attempt_id'],
                                    operation_type='run_command')
    assert cancellation._runner_never_started_handler(manager, first)
    repo.mark_operation_executing(op['operation_id'])
    assert not cancellation._runner_never_started_handler(manager, first)
    assert cancellation._runner_never_started_handler(manager, other)
    assert not cancellation._runner_never_started_handler(manager, {'attempt_id': 'unknown'})


def _batch(stop):
    return cancellation.FrozenSubagentStops((cancellation.FrozenSubagentStop(
        {"run_id": "run-123", "thread_interrupt": "attempt_signaled"}, stop,
    ),))


def _fast_cleanup(monkeypatch, runner, batch):
    """走真实 cleanup→升级主链路，只替换升级器的时间依赖。"""
    def escalate(stop):
        return _escalate(runner, stop, grace_seconds=0.2, kill_grace_seconds=0.3)

    monkeypatch.setattr(cancellation, "escalate_exclusive_runner_stop", escalate, raising=False)
    return cancellation.cleanup_subagent_stops(batch)


def test_issue_repro_live_exclusive_worker_cannot_report_ok(monkeypatch, runner):
    batch = _batch(_stop())
    assert batch.unconfirmed is False
    result = _fast_cleanup(monkeypatch, runner, batch)
    assert result["ok"] is False
    assert result["failed"] == [{
        "run_id": "run-123", "cancellation_attempted": True,
        "error_code": "RUNNER_WORKER_STILL_ALIVE",
    }]
    assert result["cancelled"][0]["pid_report"]["host_exited"] is False
    assert batch.unconfirmed is False


@pytest.mark.parametrize("exit_signal", [signal.SIGTERM, signal.SIGKILL])
def test_escalation_signals_waits_and_confirms_exit(runner, exit_signal):
    def on_kill(signum):
        if signum == exit_signal:
            # 信号返回不代表退出，后续等待中的存活观察才是证明。
            runner.on_sleep = lambda: setattr(runner, "alive", False)

    runner.on_kill = on_kill
    stop = _stop()
    result = _escalate(runner, stop, grace_seconds=0.2, kill_grace_seconds=0.3)
    expected = [signal.SIGTERM] if exit_signal == signal.SIGTERM else [signal.SIGTERM, signal.SIGKILL]
    assert result["signals"] == expected
    assert result["escalated"] is True and result["host_exited"] is True
    sends = [event for event in runner.events if event[0] == "signal"]
    assert sends[0][2] >= 0.2
    if exit_signal == signal.SIGKILL:
        assert sends[1][2] >= sends[0][2] + 0.2
    assert stop.report == {"status": "cooperative_requested", "pid": runner.pid}


def test_cooperative_exit_during_initial_grace_sends_nothing(runner):
    runner.on_sleep = lambda: setattr(runner, "alive", False)
    result = _escalate(runner)
    assert result["signals"] == [] and result["escalated"] is False
    assert result["host_exited"] is True


def test_still_alive_after_kill_is_not_exit(runner):
    result = _escalate(runner, grace_seconds=0.2, kill_grace_seconds=0.3)
    assert result["signals"] == [signal.SIGTERM, signal.SIGKILL]
    assert result["host_exited"] is False and result["identity_confirmed"] is True
    sends = [event for event in runner.events if event[0] == "signal"]
    # 和生产等待相同地比较绝对 deadline，避免 0.7-0.4 的浮点相消误差。
    assert runner.clock >= sends[-1][2] + 0.3


@pytest.mark.parametrize("status,pid,birth", [
    ("shared_host_cooperative", 99999, "birth"),
    ("identity_unavailable", 99999, "birth"),
    ("skipped", 99999, "birth"), ("no_pid", 0, ""),
    ("cooperative_requested", 0, "birth"), ("cooperative_requested", -1, "birth"),
    ("cooperative_requested", 99999, ""),
])
def test_shared_unrequested_or_unidentified_runner_never_signaled(runner, status, pid, birth):
    result = _escalate(runner, _stop(status=status, pid=pid, birth=birth))
    assert result["signals"] == [] and result["escalated"] is False
    assert not any(event[0] == "signal" for event in runner.events)


@pytest.mark.parametrize("birth,exited", [("different-birth", True), ("", False)])
def test_initial_birth_mismatch_or_unknown_never_signals(runner, birth, exited):
    runner.birth = birth
    result = _escalate(runner)
    assert result["signals"] == [] and result["host_exited"] is exited


@pytest.mark.parametrize("phase", ["before_term", "before_kill"])
@pytest.mark.parametrize("birth,exited", [("different-birth", True), ("", False)])
def test_birth_changes_during_wait_never_signals_reused_or_unknown_pid(runner, phase, birth, exited):
    def change_birth():
        if phase == "before_term" or any(event[0] == "signal" for event in runner.events):
            runner.birth = birth

    runner.on_sleep = change_birth
    result = _escalate(runner, grace_seconds=0.2, kill_grace_seconds=0.3)
    assert result["signals"] == ([] if phase == "before_term" else [signal.SIGTERM])
    assert result["host_exited"] is exited


@pytest.mark.parametrize("exception", [PermissionError("denied"), subprocess.TimeoutExpired("ps", 2), ValueError("bad birth")])
def test_unverifiable_birth_never_signals(monkeypatch, runner, exception):
    def unavailable(*args):
        raise exception

    monkeypatch.setattr(hosts, "_birth_token_matches", unavailable)
    result = _escalate(runner)
    assert result["signals"] == [] and result["host_exited"] is False
    assert result["identity_confirmed"] is False


@pytest.mark.parametrize("unknown", ["birth", "freeze"])
def test_cleanup_unknown_identity_reports_failure(monkeypatch, runner, unknown):
    stop = _stop()
    if unknown == "birth":
        runner.birth = ""
    else:
        stop = hosts.FrozenRunnerStop({"status": "identity_unavailable", "pid": runner.pid})
    batch = _batch(stop)
    result = _fast_cleanup(monkeypatch, runner, batch)
    assert result["ok"] is False
    assert result["failed"][0]["error_code"] == "RUNNER_WORKER_EXIT_UNCONFIRMED"
    assert batch.unconfirmed is False
    assert not any(event[0] == "signal" for event in runner.events)


def test_cleanup_loses_identity_after_term_is_unconfirmed_not_still_alive(monkeypatch, runner):
    runner.on_kill = lambda _: setattr(runner, "birth", "")
    result = _fast_cleanup(monkeypatch, runner, _batch(_stop()))
    assert result["ok"] is False
    assert result["failed"][0]["error_code"] == "RUNNER_WORKER_EXIT_UNCONFIRMED"
    assert result["cancelled"][0]["pid_report"]["identity_confirmed"] is False
    assert [event[1] for event in runner.events if event[0] == "signal"] == [signal.SIGTERM]


@pytest.mark.parametrize("mode", ["term_exit", "replaced", "shared", "no_pid", "skipped"])
def test_cleanup_confirmed_exit_and_protected_hosts_keep_success(monkeypatch, runner, mode):
    stop = _stop()
    if mode == "term_exit":
        runner.on_kill = lambda _: setattr(runner, "alive", False)
    elif mode == "replaced":
        runner.birth = "different-birth"
    else:
        status = "shared_host_cooperative" if mode == "shared" else mode
        stop = hosts.FrozenRunnerStop({"status": status, "pid": runner.pid})
    result = _fast_cleanup(monkeypatch, runner, _batch(stop))
    assert result["ok"] is True and result["failed"] == []
    if mode != "term_exit":
        assert not any(event[0] == "signal" for event in runner.events)


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("freeze_error,pty_error", [(False, False), (True, False), (False, True)])
def test_unconfirmed_preserves_frozen_resource_truth_table(failed, freeze_error, pty_error):
    resources = SimpleNamespace(background_freeze_error=freeze_error, pty_request_error=pty_error)
    stop = cancellation.FrozenSubagentStop({}, _stop(), resources)
    batch = cancellation.FrozenSubagentStops((stop,), ({"run_id": "bad"},) if failed else ())
    assert batch.unconfirmed is (failed or freeze_error or pty_error)


def test_cleanup_runner_stop_return_contract_is_unchanged(runner):
    stop = _stop()
    assert hosts.cleanup_runner_stop(stop) == {**stop.report, "host_exited": False, "identity_confirmed": True}
    assert runner.clock == 0 and not any(event[0] == "signal" for event in runner.events)


def test_escalation_signature_has_contract_defaults_and_keyword_only_options():
    params = inspect.signature(hosts.escalate_exclusive_runner_stop).parameters
    assert params["grace_seconds"].default == 1.5
    assert params["kill_grace_seconds"].default == 1.5
    assert all(params[name].kind == inspect.Parameter.KEYWORD_ONLY for name in (
        "grace_seconds", "kill_grace_seconds", "sleep", "now",
    ))


@pytest.mark.parametrize("capture_number,expected", [(2, []), (4, [signal.SIGTERM])])
def test_birth_rechecked_immediately_before_each_signal(monkeypatch, runner, capture_number, expected):
    calls = []

    def capture(pid):
        assert pid == runner.pid
        calls.append(pid)
        return "" if len(calls) == capture_number else "birth"

    monkeypatch.setattr(hosts, "capture_process_birth_token", capture)
    result = _escalate(runner, grace_seconds=0, kill_grace_seconds=0)
    assert result["signals"] == expected
    assert result["host_exited"] is False and result["identity_confirmed"] is False


@pytest.mark.parametrize("failure", [PermissionError("denied"), ProcessLookupError("gone")])
def test_signal_failure_must_reobserve_not_claim_exit(monkeypatch, runner, failure):
    def kill(pid, signum):
        assert pid == runner.pid and signum == signal.SIGTERM
        runner.events.append(("failed_signal", signum))
        raise failure

    monkeypatch.setattr("os.kill", kill)
    result = _escalate(runner, grace_seconds=0, kill_grace_seconds=0)
    assert result["signals"] == [] and result["host_exited"] is False
    assert result["signal_error_type"] == type(failure).__name__
    assert runner.events[-1] == ("verify", "birth")


def test_cleanup_probe_exception_is_structured_exit_unconfirmed(monkeypatch, runner):
    def capture(pid):
        assert pid == runner.pid
        raise PermissionError("birth observation denied")

    monkeypatch.setattr(hosts, "capture_process_birth_token", capture)
    result = _fast_cleanup(monkeypatch, runner, _batch(_stop()))
    assert result["ok"] is False
    assert result["failed"][0]["error_code"] == "RUNNER_WORKER_EXIT_UNCONFIRMED"
    assert result["cancelled"][0]["pid_report"]["identity_confirmed"] is False
    assert not any(event[0] == "signal" for event in runner.events)
