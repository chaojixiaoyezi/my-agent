"""原宿主凭据与身份头歧义必须分开裁决；真实HTTP保留同名副本，不绕handler或执行模型。"""
import http.client
import json
import secrets
from email.message import Message
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import http_handlers
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.settings import AgentConfig


# LLM: 临时EchoAgent及random HTTP监听器走原启动/认证/入站路径，不发IM，不运行Gateway请求worker。
# 函数用途: 给一次HTTP身份试验提供真正的宿主凭据和独立数据根，finally停止本测试监听器。
@pytest.fixture
def server(tmp_path):
    config = AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'), enable_plugins=False, enable_subagents=False)
    agent = SimpleAgent(config, tmp_path / 'workspace')
    paths = gateway_paths(agent)
    paths.inbox.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    auth = AuthMiddleware(AuthManager(auth_enabled=True, admin_user_id='admin'), auth_token=token)
    listener = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent, auth_middleware=auth))
    listener.start()
    try:
        yield listener, agent, token
    finally:
        listener.stop()


# LLM: http.client逐个写真实重复头，不能先dict合并；只发合成短任务且没有执行worker。
# 函数用途: 让原handler处理完整HTTP头副本，返回code和原队列增长供零业务断言。
def _ask(server, headers):
    listener, agent, _ = server
    port = listener.server.server_address[1]
    body = json.dumps({'kind': 'ask', 'goal': '合成短任务', 'conversation_id': 'synthetic-own'}).encode()
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        connection.putrequest('POST', '/ask')
        connection.putheader('Content-Type', 'application/json')
        connection.putheader('Content-Length', str(len(body)))
        for name, value in headers:
            connection.putheader(name, value)
        connection.endheaders(body)
        response = connection.getresponse()
        status = response.status
        result = json.loads(response.read())
        if status == 403:
            assert result.get("error_code") != "LOCAL_CREDENTIAL_REQUIRED"
        return status, len(tuple(gateway_paths(agent).inbox.glob('*.json')))
    finally:
        connection.close()


@pytest.mark.parametrize('different_case', [False, True])
@pytest.mark.parametrize('conflict', ['user', 'channel', 'both', 'credential'])
def test_conflicting_header_copies_are_denied_without_a_business_queue(server, different_case, conflict):
    _, _, token = server
    rows = [('X-Gateway-Token', token), ('X-User-Id', 'ou_ordinary'), ('X-Channel', 'feishu')]
    if conflict in {'user', 'both'}:
        rows.append(('x-user-id' if different_case else 'X-User-Id', 'admin'))
    if conflict in {'channel', 'both'}:
        rows.append(('x-channel' if different_case else 'X-Channel', 'chat'))
    if conflict == 'credential':
        rows.append(('x-gateway-token' if different_case else 'X-Gateway-Token', 'owned-invalid'))
    status, queued = _ask(server, rows)
    assert status == 403 and queued == 0


def test_valid_host_credential_with_no_identity_remains_legal_local_terminal(server):
    listener, _, token = server
    assert listener.auth_middleware.extract_identity({'X-Gateway-Token': token}) == ('admin', 'chat')
    assert _ask(server, [('X-Gateway-Token', token)]) == (202, 1)


def test_same_value_copies_keep_the_original_ordinary_identity(server):
    _, _, token = server
    assert _ask(server, [('X-Gateway-Token', token), ('X-User-Id', 'ou_ordinary'), ('x-user-id', 'ou_ordinary'),
        ('X-Channel', 'feishu'), ('x-channel', 'feishu')]) == (202, 1)


def test_handler_identity_helpers_do_not_collapse_raw_copies_into_local_admin():
    auth = AuthMiddleware(AuthManager(auth_enabled=True), auth_token='synthetic')
    headers = Message()
    for name, value in [('X-Gateway-Token', 'synthetic'), ('X-User-Id', ''), ('X-User-Id', 'ordinary')]:
        headers[name] = value
    handler = SimpleNamespace(_auth_middleware=auth, headers=headers, client_address=('127.0.0.1', 123))
    user, permission = http_handlers._request_identity(handler)
    assert user == 'anonymous'
    assert permission.role.value == 'user'
    assert http_handlers._request_channel(handler) == ('anonymous', 'external')


def test_handler_denial_helper_does_not_label_credential_conflict_as_missing():
    auth = AuthMiddleware(AuthManager(auth_enabled=True), auth_token='synthetic')
    headers = Message()
    headers['X-Gateway-Token'] = 'wrong'
    headers['X-Gateway-Token'] = 'synthetic'
    handler = SimpleNamespace(_auth_middleware=auth, headers=headers, client_address=('127.0.0.1', 123))
    assert http_handlers._denial_body(handler, {'error': 'forbidden'}) == {'error': 'forbidden'}
