"""真实受管服务不能因调用方漏middleware而跳过认证；无鉴权只保留明确单用户无IM部署。"""
import http.client
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli import gateway_process


# LLM: 全部数据根与echo配置由测试创建，HTTP监听器用原服务，无Gateway worker或真实凭据。
# 函数用途: 创建准确宿主身份以验证原生命周期配置门。
def _agent(tmp_path, **values):
    return SimpleAgent(AgentConfig(model_backend='echo', enable_plugins=False, enable_subagents=False,
        my_agent_home=str(tmp_path/'home'), **values), tmp_path/'workspace')


def _uncredentialed_ask(server):
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=3)
    try:
        connection.request('POST', '/ask', b'{"goal":"synthetic"}', {'Content-Type':'application/json'})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def test_actual_auth_enabled_agent_without_injected_middleware_remains_protected(tmp_path):
    agent = _agent(tmp_path, auth_enabled=True)
    paths = gateway_paths(agent)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        assert _uncredentialed_ask(server) == 403
        assert server.auth_middleware is not None
        assert not tuple(paths.inbox.glob('*.json'))
    finally:
        server.stop()


@pytest.mark.parametrize('unsafe', ['owners', 'feishu_app_id', 'feishu_app_secret', 'feishu_verification_token',
                                   'feishu_encrypt_key', 'qq_app_id', 'qq_app_secret'])
def test_explicit_no_auth_rejects_multiowner_or_any_declared_im_before_g1_or_socket(tmp_path, unsafe):
    values = dict(auth_enabled=False, gateway_per_user_owner_scoping=unsafe=='owners')
    if unsafe != 'owners':
        values[unsafe] = 'OWNED_DECLARED_SECRET'
    agent = _agent(tmp_path, **values)
    server = GatewayHTTPServer(0, gateway_paths(agent), params=GatewayHTTPServerParams(agent=agent))
    try:
        with pytest.raises(RuntimeError) as failure:
            server.start()
    finally:
        server.stop()
    assert getattr(failure.value, 'error_code', '') == 'AUTH_CONFIGURATION_UNSAFE'
    assert 'OWNED_DECLARED_SECRET' not in str(failure.value)
    assert server.server is None
    assert not (agent.home_paths.root/'secrets/gateway-local-client-token').exists()
    server.stop()


def test_explicit_no_auth_single_local_preserves_original_boundary(tmp_path):
    agent = _agent(tmp_path, auth_enabled=False, gateway_per_user_owner_scoping=False)
    paths = gateway_paths(agent)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        assert server.auth_middleware is None
        assert _uncredentialed_ask(server) == 202
    finally:
        server.stop()


@pytest.mark.parametrize('values', [dict(auth_enabled=False, gateway_per_user_owner_scoping=True),
    dict(auth_enabled=True, gateway_per_user_owner_scoping=False, feishu_app_id='SYNTHETIC_APP'),
    dict(auth_enabled=True, gateway_per_user_owner_scoping=False, qq_app_id='SYNTHETIC_APP')])
def test_cli_rejects_unsafe_auth_configuration_before_recovery_or_threads(tmp_path, monkeypatch, capsys, values):
    agent = _agent(tmp_path, **values)
    config_path = tmp_path / 'unsafe-cli.yaml'
    config_path.write_text(
        'model_backend: echo\nenable_plugins: false\nenable_subagents: false\ngateway_worker_process_count: 0\n'
        + f'my_agent_home: {tmp_path / "home"}\n'
        + ''.join(f'{key}: {str(value).lower() if isinstance(value, bool) else value}\n' for key, value in values.items()),
        encoding='utf-8',
    )
    monkeypatch.setattr(gateway_process, 'make_agent', lambda _: agent)
    monkeypatch.setattr(gateway_process, 'mark_hosting_gateway_process', lambda: None)
    monkeypatch.setattr(gateway_process, 'wait_for_predecessor_exit', lambda _: True)
    def must_not_run(*_):
        pytest.fail('配置不安全时不允许启动恢复或线程')
    monkeypatch.setattr(gateway_process, '_cmd_gateway_run_setup', must_not_run)
    monkeypatch.setattr(gateway_process, '_cmd_gateway_run_threads', must_not_run)
    assert gateway_process.cmd_gateway_run(SimpleNamespace(config=str(config_path), after_pid=0)) == 2
    assert 'AUTH_CONFIGURATION_UNSAFE' in capsys.readouterr().err
    assert not (agent.home_paths.root/'secrets/gateway-local-client-token').exists()


@pytest.mark.parametrize('disabled_value', [False, None, 0, ''])
def test_auth_enabled_rejects_explicit_disabled_manager_before_socket_or_g1(tmp_path, disabled_value):
    agent = _agent(tmp_path, auth_enabled=True)
    middleware = AuthMiddleware(AuthManager(auth_enabled=disabled_value), auth_token='SYNTHETIC_CUSTOM_TOKEN')
    server = GatewayHTTPServer(0, gateway_paths(agent), params=GatewayHTTPServerParams(agent=agent, auth_middleware=middleware))
    try:
        with pytest.raises(RuntimeError) as failure:
            server.start()
    finally:
        server.stop()
    assert getattr(failure.value, 'error_code', '') == 'AUTH_CONFIGURATION_UNSAFE'
    assert getattr(failure.value, 'reason_code', '') == 'middleware_disabled'
    assert server.auth_middleware is middleware
    assert server.server is None
    assert not (agent.home_paths.root/'secrets/gateway-local-client-token').exists()


def test_auth_enabled_preserves_explicit_enabled_middleware_and_custom_token(tmp_path):
    agent = _agent(tmp_path, auth_enabled=True)
    middleware = AuthMiddleware(AuthManager(auth_enabled=True), auth_token='SYNTHETIC_CUSTOM_TOKEN')
    server = GatewayHTTPServer(0, gateway_paths(agent), params=GatewayHTTPServerParams(agent=agent, auth_middleware=middleware))
    server.paths.inbox.mkdir(parents=True, exist_ok=True)
    server.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=3)
    try:
        connection.request('POST', '/ask', b'{"goal":"synthetic"}', {'Content-Type':'application/json',
                           'X-Gateway-Token':'SYNTHETIC_CUSTOM_TOKEN'})
        response = connection.getresponse()
        response.read()
        assert response.status == 202
        assert server.auth_middleware is middleware
        assert len(tuple(server.paths.inbox.glob('*.json'))) == 1
    finally:
        connection.close()
        server.stop()


@pytest.mark.parametrize('field', ['feishu_app_id', 'feishu_app_secret', 'feishu_verification_token',
                                  'feishu_encrypt_key', 'qq_app_id', 'qq_app_secret'])
def test_im_without_owner_scoping_is_rejected_even_with_enabled_auth(tmp_path, field):
    agent = _agent(tmp_path, auth_enabled=True, gateway_per_user_owner_scoping=False, **{field:'OWNED_DECLARED_SECRET'})
    server = GatewayHTTPServer(0, gateway_paths(agent), params=GatewayHTTPServerParams(agent=agent))
    try:
        with pytest.raises(RuntimeError) as failure:
            server.start()
    finally:
        server.stop()
    assert getattr(failure.value, 'error_code', '') == 'AUTH_CONFIGURATION_UNSAFE'
    assert getattr(failure.value, 'reason_code', '') == 'im_without_owner_scoping'
    assert 'OWNED_DECLARED_SECRET' not in str(failure.value)
    assert server.server is None
    assert not (agent.home_paths.root/'secrets/gateway-local-client-token').exists()


@pytest.mark.parametrize('provider', ['feishu', 'qq'])
def test_im_with_scoping_and_auth_preserves_original_protected_start(tmp_path, provider):
    agent = _agent(tmp_path, auth_enabled=True, gateway_per_user_owner_scoping=True,
                   **{provider+'_app_id':'SYNTHETIC_APP', provider+'_app_secret':'SYNTHETIC_SECRET'})
    paths=gateway_paths(agent)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        assert server.auth_middleware is not None
        assert _uncredentialed_ask(server) == 403
        assert not tuple(paths.inbox.glob('*.json'))
    finally:
        server.stop()


@pytest.mark.parametrize('false_equivalent', [None, 0, ''])
def test_library_false_equivalent_owner_scoping_cannot_bypass_the_im_gate(tmp_path, false_equivalent):
    agent = _agent(tmp_path, auth_enabled=True, gateway_per_user_owner_scoping=false_equivalent, feishu_app_id='SYNTHETIC_APP')
    server = GatewayHTTPServer(0, gateway_paths(agent), params=GatewayHTTPServerParams(agent=agent))
    try:
        with pytest.raises(RuntimeError) as failure:
            server.start()
    finally:
        server.stop()
    assert getattr(failure.value, 'error_code', '') == 'AUTH_CONFIGURATION_UNSAFE'
    assert getattr(failure.value, 'reason_code', '') == 'im_without_owner_scoping'
    assert server.server is None
    assert not (agent.home_paths.root/'secrets/gateway-local-client-token').exists()
