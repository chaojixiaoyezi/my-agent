"""W0 与后续分片共用的真实 Gateway、隔离 owner 和原生 JSON 测试入口。"""
import http.client
import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.gateway_parts import business_api as api

_PROVIDER_BINDINGS = {}


# LLM: 每测试绑定独立home与数据根，不触碰真实宿主运行账。
# 函数用途: 提供直接调用用的最小可信宿主。
@pytest.fixture
def v13_host(tmp_path):
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )
    paths = home_paths(tmp_path / 'host-home')
    return SimpleNamespace(home_paths=home_paths_with_owner(paths, resolve_owner_home(paths.root)),
        config=SimpleNamespace(business_spaces_enabled=True), business_v13_providers=dict(_PROVIDER_BINDINGS))


# LLM: 原服务必须真实启动，echo模型不发外网；finally关闭HTTP并恢复环境。
# 函数用途: 提供真实Gateway与SimpleAgent的隔离夹具。
@pytest.fixture
def v13_native(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig
    root = tmp_path / 'native-home'
    monkeypatch.setenv('MY_AGENT_HOME', str(root))
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=False,
        business_spaces_enabled=True, memory_path='memory.jsonl'), tmp_path / 'workspace')
    agent.business_v13_providers = dict(_PROVIDER_BINDINGS)
    server = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / 'gateway'),
        params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        yield server, agent
    finally:
        server.stop()


# LLM: 只测试宿主凭据真实入站，不往JSON塞身份字段，连接由调用局部管理。
# 函数用途: 经真实HTTP调用原生业务动作并解码标准信封。
def v13_call(server, action, params, *, user, idempotency_key=None, expected_revision=None):
    server = server[0] if isinstance(server, tuple) else server
    request = {'action': action, 'request_id': 'v13-' + uuid4().hex, 'params': params}
    if idempotency_key is not None:
        request['idempotency_key'] = idempotency_key
    if expected_revision is not None:
        request['expected_revision'] = expected_revision
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=20)
    headers = {'Content-Type': 'application/json', 'X-Gateway-Token': server._local_client_credential,
        'X-User-Id': user, 'X-Channel': 'feishu'}
    try:
        connection.request('POST', '/client/business', json.dumps(request), headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


# LLM: 数据经真实Store事务写入，不手写生产文件形状。
# 函数用途: 为分片测试种入规范集合记录。
def v13_seed(store_owner_home, collection, records):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    store = V13Store(store_owner_home)

    # LLM: 传字典时键就是规范ID；数组只接受记录中明确的ID字段。
    # 函数用途: 写入测试提供的规范记录并返回数量。
    def mutate(rows):
        if isinstance(records, dict):
            rows.update(records)
        else:
            for record in records:
                keys = [key for key in record if key.endswith('_id') and key != 'owner_id']
                if collection == 'assistant_agents':
                    keys = ['space_id']
                if not keys:
                    raise ValueError('测试记录缺规范ID')
                rows[record[keys[0]]] = record
        return {'count': len(rows)}

    return store.transaction(collection, mutate, idempotency_key='seed-' + uuid4().hex,
        params_digest='seed-' + uuid4().hex)


# LLM: 空间经真实W1入口创建，不把虚构ID塞进其他分片测试。
# 函数用途: 创建当前测试用户的原生业务空间。
def v13_space(server):
    status, body = v13_call(server, 'spaces.create', {'name': 'v13测试空间'},
        user='business-alice', idempotency_key='v13-space-' + uuid4().hex)
    assert status == 200, body
    return body['data']['space_id']


# LLM: 仅夹具创建期间复制绑定，不安装真实外部提供者或发网络请求。
# 函数用途: 在测试上下文登记合成provider。
@contextmanager
def v13_fake_provider(name, obj):
    previous = dict(_PROVIDER_BINDINGS)
    _PROVIDER_BINDINGS[name] = obj
    try:
        yield obj
    finally:
        _PROVIDER_BINDINGS.clear()
        _PROVIDER_BINDINGS.update(previous)


# LLM: 既验证真实HTTP也验证闭合信封；没有手写替代响应。
# 函数用途: 证明共享底座可启动并完成扩展发现调用。
def test_harness_starts_real_gateway(v13_native):
    status, body = v13_call(v13_native, 'system.capabilities.extensions', {}, user='business-alice')
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    assert 'system.capabilities.extensions' in body['data']['supported_actions']
    assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
        'allowed_actions', 'warnings', 'effect_outcome'}
