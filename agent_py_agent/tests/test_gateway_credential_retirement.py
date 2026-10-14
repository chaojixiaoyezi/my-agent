"""安全追加：旧配置不得重新打开无凭据回环信任，仅使用临时根与随机端口。"""
from __future__ import annotations

import http.client
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.client_credentials import gateway_client_credentials
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
    GatewayLocalCredentialRequired,
)
from agent_py_agent.agent.gateway_parts.local_client_token import (
    LocalClientCredentialError,
    load_local_client_credential,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli.gateway_process import (
    _build_gateway_auth_middleware,
    _preflight_required_local_credential,
)


# LLM: 仅构造真实数据根布局的隔离宿主，旧上线开关用于模拟存量 YAML，不能当授权事实。
# 函数用途: 让原 G1 凭据和 CLI 启动入口在测试临时根工作，不读取用户主目录。
def host(root):
    return SimpleNamespace(config=SimpleNamespace(auth_enabled=True, gateway_require_local_credential=False),
        home_paths=SimpleNamespace(root=root, owner_home_dir=root / 'owners/local/main'))


# LLM: 请求没有宿主凭据，字段都是可伪造输入；只访问测试随机端口，不运行模型或真实监控。
# 函数用途: 读取原 HTTP 鉴权响应，连接在所有分支关闭。
def request(port, method, path, headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        connection.request(method, path, body=b'{}' if method == 'POST' else None, headers=headers or {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize('peer', ['127.0.0.1', '::1', None])
def test_old_false_setting_cannot_restore_uncredentialed_identity(peer):
    middleware = _build_gateway_auth_middleware(SimpleNamespace(auth_enabled=True, gateway_require_local_credential=False))
    assert middleware.extract_identity({'X-Channel': 'chat', 'X-User-Id': 'admin'}, peer) == ('anonymous', 'external')
    assert middleware.check_trusted({}, peer) is False


def test_old_false_setting_client_missing_credential_is_zero_request(tmp_path):
    with pytest.raises(LocalClientCredentialError):
        gateway_client_credentials(host(tmp_path / 'data')).headers()


def test_old_false_setting_startup_preflight_prepares_stable_credential(tmp_path):
    agent = host(tmp_path / 'data')
    _preflight_required_local_credential(agent)
    first = load_local_client_credential(agent.home_paths.root)
    _preflight_required_local_credential(agent)
    assert load_local_client_credential(agent.home_paths.root) == first


def test_startup_preflight_rejects_broken_credential_even_with_old_false(tmp_path):
    agent = host(tmp_path / 'data')
    secrets = agent.home_paths.root / 'secrets'
    secrets.mkdir(parents=True, mode=0o700)
    from agent_py_agent.agent.gateway_parts.local_client_token import local_client_credential_path
    path = local_client_credential_path(agent.home_paths.root)
    path.write_text('broken', encoding='utf-8')
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(GatewayLocalCredentialRequired):
        _preflight_required_local_credential(agent)
    assert path.read_bytes() == before


@pytest.mark.parametrize('method,path', [('POST', '/ask'), ('POST', '/stop'), ('POST', '/client/history')])
def test_real_http_rejects_uncredentialed_local_privileged_request(tmp_path, method, path):
    agent = host(tmp_path / 'data')
    middleware = _build_gateway_auth_middleware(agent.config)
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / 'queue'),
        params=GatewayHTTPServerParams(agent=agent, auth_middleware=middleware))
    server.start()
    try:
        port = server.server.server_address[1]
        status, body = request(port, method, path, {'X-Channel': 'chat', 'X-User-Id': 'admin'})
        assert status == 403
        assert body.get('error_code') == 'LOCAL_CREDENTIAL_REQUIRED'
        assert request(port, 'GET', '/status')[0] == 200
    finally:
        server.stop()
