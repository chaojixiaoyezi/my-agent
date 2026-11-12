"""原 NetworkGatewayObserver→BusinessClient 的错误回执运输，不放宽业务封套。

合法拒绝走真实 Gateway 路由；坏字节走回环故障服务；读取异常仅注入最低 HTTP 流。
"""
from __future__ import annotations

import copy
import io
import json
import threading
import urllib.error
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import authenticated_http
from agent_py_agent.agent.gateway_parts.business_api import _response
from agent_py_agent.cli.chat_parts.gateway_client import (
    STREAM_CHUNK_READ_MAX_BYTES,
    NetworkGatewayObserver,
)
from agent_py_agent.cli.space_commands import BusinessClient


def _client(observer):
    return BusinessClient(lambda route, payload, timeout: observer.request_json(
        'POST', route, payload, total_deadline_seconds=timeout))


@pytest.fixture
def error_streams(monkeypatch):
    """旁观真实 opener 抛出的原异常，既不替换正文，也不补关闭。"""
    opened = authenticated_http.open_authenticated_request
    errors = []

    def observe(request, *, timeout):
        try:
            return opened(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            errors.append(error)
            raise

    monkeypatch.setattr(authenticated_http, 'open_authenticated_request', observe)
    return errors


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.settings import AgentConfig

    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    agent = SimpleAgent(AgentConfig(model_backend='echo', business_spaces_enabled=True,
        my_agent_home=str(home)), tmp_path / 'workspace')
    monkeypatch.setattr(agent, 'run', lambda *a, **k: pytest.fail('错误回执测试不得调用模型'))
    server = GatewayHTTPServer(0, gateway_paths_from_root(home / 'gateway'),
        params=GatewayHTTPServerParams(agent=agent))
    server.start()
    agent.gateway_network_headers = lambda: {'X-Gateway-Token': server._local_client_credential}
    observer = NetworkGatewayObserver(agent, f'http://127.0.0.1:{server.server.server_address[1]}')
    try:
        yield agent, _client(observer)
    finally:
        server.stop()


@pytest.mark.parametrize('status,code', [
    (403, 'ACTOR_REQUIRED'), (409, 'IDEMPOTENCY_CONFLICT'), (503, 'HOST_NOT_READY'),
])
def test_gateway_rejection_preserves_business_receipt(gateway, error_streams, status, code):
    agent, client = gateway
    if status == 403:
        prepared = client.prepare('background_agents.list', {'limit': 5})
    elif status == 503:
        agent.config.business_spaces_enabled = False
        prepared = client.prepare('spaces.list', {'limit': 5})
    else:
        first = client.prepare('spaces.create', {'name': '原名'}, idempotency_key='error-wire-same-key-0001')
        assert client.send(first)['ok']
        prepared = client.prepare('spaces.create', {'name': '不同名'}, idempotency_key='error-wire-same-key-0001')
    original = prepared.payload_json
    result = client.send(prepared)
    assert result['http_status'] == status, result
    assert result['error']['code'] == code, result
    assert result['state'] == 'failed' and result['effect_outcome'] == 'not_started', result
    assert not result['ok'] and result['request_id'] == prepared.payload()['request_id']
    assert result['operation_id'] is None and result['data'] == {}
    assert result['error']['retryable'] is (status == 503)
    assert prepared.payload_json == original
    assert len(error_streams) == 1 and error_streams[0].closed


@pytest.fixture
def wire_server():
    """只提供坏字节/跳转故障，不模拟授权；规范封套来自原服务生产者。"""
    state = SimpleNamespace(status=403, body=b'', redirect=False, requests=[])

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            state.requests.append((self.path, self.headers, json.loads(raw)))
            self.send_response(state.status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(state.body)))
            if state.redirect:
                self.send_header('Location', '/redirect-target')
            self.end_headers()
            self.wfile.write(state.body)

        def do_GET(self):
            state.requests.append((self.path, self.headers, None))
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    state.url = f'http://127.0.0.1:{server.server_port}'
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
        assert not worker.is_alive()


def _observer(url):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    agent = SimpleNamespace(config=SimpleNamespace(gateway_request_timeout=2),
        owner_identity=OwnerIdentity.local_main(),
        gateway_request_identity=lambda: {'user_id': 'wire-user', 'channel': 'wire-channel',
            'channel_chat_type': 'group', 'channel_chat_id': 'wire-group'},
        gateway_network_headers=lambda: {'X-Gateway-Token': 'isolated-test-token',
            'X-User-Id': 'must-not-replace-original'})
    return NetworkGatewayObserver(agent, url)


def _refusal(request_id):
    return _response(request_id, error={'code': 'CAPABILITY_DENIED', 'message': '拒绝',
        'retryable': False, 'details': []}, effect='not_started')


@pytest.mark.parametrize('bad', ['utf8', 'json', 'non_object', 'over_limit', 'empty'])
def test_bad_http_body_stays_unknown_and_closes(wire_server, error_streams, bad):
    client = _client(_observer(wire_server.url))
    prepared = client.prepare('spaces.list', {})
    raw = json.dumps(_refusal(prepared.payload()['request_id']), ensure_ascii=False).encode()
    wire_server.body = {
        # 替换解码会产生一个仍符合schema的假拒绝，因此必须严格UTF-8。
        'utf8': raw.replace('拒绝'.encode(), b'\xff'),
        'json': raw[:-1], 'non_object': b'[]', 'empty': b'',
        # 合法JSON加空白超限也必须拒绝，不能因前缀是合法封套而采信。
        'over_limit': raw + b' ' * (STREAM_CHUNK_READ_MAX_BYTES + 1 - len(raw)),
    }[bad]
    result = client.send(prepared)
    assert result['http_status'] == 403 and not result['ok'], result
    assert result['state'] == 'unknown' and result['effect_outcome'] == 'unknown', result
    assert result['error']['code'] == 'BUSINESS_RESPONSE_INVALID'
    assert result['request_id'] == prepared.payload()['request_id']
    assert len(wire_server.requests) == 1
    assert len(error_streams) == 1 and error_streams[0].closed


@pytest.mark.parametrize('status', [200, 403])
def test_exact_read_limit_and_identity_payload_contract(wire_server, error_streams, status):
    observer = _observer(wire_server.url)
    wire_server.status = status
    wire_server.body = b'{"message":"ok"}' + b' ' * (STREAM_CHUNK_READ_MAX_BYTES - len(b'{"message":"ok"}'))
    payload = {'action': 'spaces.list', 'params': {}, 'request_id': 'original-request'}
    original = copy.deepcopy(payload)
    result = observer.request_json('POST', '/client/business', payload,
        total_deadline_seconds=2, conversation_id='original-session')
    assert result == (status, {'message': 'ok'})
    assert payload == original
    assert len(wire_server.requests) == 1
    path, headers, sent = wire_server.requests[0]
    assert path == '/client/business' and sent == original
    assert headers['X-User-Id'] == 'wire-user' and headers['X-Channel'] == 'wire-channel'
    assert headers['X-Channel-Chat-Type'] == 'group' and headers['X-Channel-Chat-Id'] == 'wire-group'
    assert headers['X-Conversation-Id'] == 'original-session'
    assert headers.get_all('X-Gateway-Token') == ['isolated-test-token']
    assert all(error.closed for error in error_streams)


@pytest.mark.parametrize('status', [302, 307, 308])
def test_authenticated_redirect_does_not_send_second_request(wire_server, error_streams, status):
    wire_server.status, wire_server.redirect, wire_server.body = status, True, b'{}'
    result = _observer(wire_server.url).request_json('POST', '/client/business', {'original': 'payload'})
    assert result == (status, {})
    assert [path for path, _, _ in wire_server.requests] == ['/client/business']
    assert len(error_streams) == 1 and error_streams[0].closed


@pytest.mark.parametrize('fault', ['valid', 'oversize', 'oserror', 'timeout', 'incomplete'])
def test_http_error_stream_bound_close_and_no_retry(monkeypatch, fault):
    observer = _observer('http://127.0.0.1:1')
    client = _client(observer)
    prepared = client.prepare('spaces.list', {})
    raw = json.dumps(_refusal(prepared.payload()['request_id'])).encode()

    class Stream(io.BytesIO):
        def __init__(self):
            super().__init__(raw + (b' ' * STREAM_CHUNK_READ_MAX_BYTES if fault == 'oversize' else b''))
            self.read_sizes, self.close_count = [], 0

        def read(self, size=-1):
            self.read_sizes.append(size)
            if fault == 'oserror':
                raise OSError('injected read failure')
            if fault == 'timeout':
                raise TimeoutError('injected timeout')
            if fault == 'incomplete':
                raise IncompleteRead(b'{', 20)
            return super().read(size)

        def close(self):
            self.close_count += 1
            super().close()

    stream = Stream()
    error = urllib.error.HTTPError(observer.base_url, 403, 'isolated failure', {}, stream)
    calls = []

    def fail(request, *, timeout):
        calls.append((request, timeout))
        raise error

    monkeypatch.setattr(authenticated_http, 'open_authenticated_request', fail)
    result = client.send(prepared)
    assert result['http_status'] == 403 and not result['ok'], result
    if fault == 'valid':
        assert result['error']['code'] == 'CAPABILITY_DENIED' and result['effect_outcome'] == 'not_started'
    else:
        assert result['error']['code'] == 'BUSINESS_RESPONSE_INVALID' and result['effect_outcome'] == 'unknown'
    assert stream.read_sizes == [STREAM_CHUNK_READ_MAX_BYTES + 1]
    assert stream.closed and stream.close_count == 1
    assert len(calls) == 1 and calls[0][1] == 10.0


@pytest.mark.parametrize('deadline,expected', [(None, 2.0), (0.01, 0.1), (1.25, 1.25)])
def test_timeout_contract_unchanged(monkeypatch, deadline, expected):
    observer = _observer('http://127.0.0.1:1')
    stream = io.BytesIO(b'{}')
    error = urllib.error.HTTPError(observer.base_url, 503, 'isolated failure', {}, stream)
    calls = []

    def fail(request, *, timeout):
        calls.append(timeout)
        raise error

    monkeypatch.setattr(authenticated_http, 'open_authenticated_request', fail)
    assert observer.request_json('POST', '/ask', {}, total_deadline_seconds=deadline) == (503, {})
    assert calls == [expected] and stream.closed
