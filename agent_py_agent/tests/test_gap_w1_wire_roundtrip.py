"""真实 HTTP 客户端经原 Gateway 路由、鉴权和业务信封的往返回归；仅模型 I/O 使用假后端。"""
from __future__ import annotations

import http.client
import json

import pytest

from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.plugin_host.schemas import REQUEST_MAX_BYTES
from agent_py_agent.agent.settings import model_scope
from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from agent_py_agent.tests.test_gap_w1_openai_compat import FakeBackend, FakeOwner, add_profile, body


@pytest.fixture
def wire(tmp_path, monkeypatch):
    seen, owners = [], {}
    root = tmp_path / 'home'
    base = FakeOwner(root)
    base.config.auth_enabled = True
    base.config.gateway_per_user_owner_scoping = True
    base.config.gateway_user_inflight_limit = 1

    def resolve(_base, owner, stack=None):
        if owner.owner_id not in owners:
            paths = home_paths_with_owner(base.home_paths, resolve_owner_home(root, owner))
            host = FakeOwner(root, paths)
            host.config.gateway_per_user_owner_scoping = True
            owners[owner.owner_id] = (host, add_profile(host, 'private-' + owner.owner_id.rsplit('/', 1)[-1]))
        return owners[owner.owner_id][0]

    monkeypatch.setattr(request_worker, '_resolve_request_agent_for_owner', resolve)
    monkeypatch.setattr(model_scope, '_profile_backend', lambda host, cfg: FakeBackend(host, cfg, seen))
    server = GatewayHTTPServer(0, gateway_paths_from_root(root / 'gateway'),
        params=GatewayHTTPServerParams(agent=base))
    server.start()
    try:
        yield server, base, seen, owners, resolve
    finally:
        server.stop()


def request(wire, path, payload, *, user='alice', token='bearer', extra=(), raw=None):
    server = wire[0]
    data = json.dumps(payload).encode() if raw is None else raw
    headers = [('Content-Type', 'application/json'), ('Content-Length', str(len(data))),
               ('X-User-Id', user), ('X-Channel', 'feishu')]
    if token == 'bearer':
        headers.append(('Authorization', 'Bearer ' + server._local_client_credential))
    elif token == 'gateway':
        headers.append(('X-Gateway-Token', server._local_client_credential))
    elif token:
        headers.append(('Authorization', 'Bearer ' + token))
    headers.extend(extra)
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=10)
    try:
        connection.putrequest('POST', path)
        for key, value in headers:
            connection.putheader(key, value)
        connection.endheaders(data)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def business(wire, action, params=None, **kwargs):
    payload = {'action': action, 'request_id': 'wire-business-request', 'params': params or {}}
    if action in {'llm_records.delete', 'llm.gateway.test', 'llm.gateway.chat_completions'}:
        payload['idempotency_key'] = 'wire-unique-action-key-0001'
    status, headers, raw = request(wire, '/client/business', payload, token='gateway', **kwargs)
    return status, headers, json.loads(raw)


def test_actual_http_chat_test_and_records_business_roundtrip(wire):
    status, _, raw = request(wire, '/v1/chat/completions', body('private-alice'))
    completion = json.loads(raw)
    assert status == 200, raw
    assert completion['object'] == 'chat.completion' and completion['model'] == 'private-alice'
    assert completion['usage']['total_tokens'] == 11
    status, _, raw = request(wire, '/api/llm/test', {})
    result = json.loads(raw)
    assert status == 200 and result['ok'] and result['model'] == 'private-alice', raw
    status, _, envelope = business(wire, 'llm_records.list')
    assert status == 200 and envelope['state'] == 'completed' and envelope['error'] is None, envelope
    rows = envelope['data']['items']
    assert len(rows) == 2 and {row['purpose'] for row in rows} == {'openai_compat'}
    assert all(row['status'] == 'completed' for row in rows)
    serialized = json.dumps(rows)
    assert 'Reply OK.' not in serialized and 'hello' not in serialized
    status, _, envelope = business(wire, 'llm.gateway.test')
    assert status == 200 and envelope['data']['ok'], envelope
    assert wire[2][-1]['options'].max_output_tokens == 16


def test_actual_buffered_sse_frames(wire):
    status, headers, raw = request(wire, '/v1/chat/completions', body('private-alice', stream=True))
    assert status == 200 and headers['X-My-Agent-Stream-Mode'] == 'buffered', raw
    assert headers['Content-Type'].startswith('text/event-stream')
    frames = raw.decode().strip().split('\n\n')
    assert frames[-1] == 'data: [DONE]'
    chunks = [json.loads(frame[6:]) for frame in frames[:-1]]
    assert chunks[0]['choices'][0]['delta'] == {'role': 'assistant'}
    assert ''.join(row['choices'][0]['delta'].get('content', '') for row in chunks) == '你好，世界。'
    assert chunks[-1]['choices'][0]['finish_reason'] == 'stop'
    assert len(wire[2]) == 1


@pytest.mark.parametrize('path', ['/v1/chat/completions', '/api/llm/test'])
def test_actual_auth_errors_and_credential_alias_conflict(wire, path):
    payload = {} if path.endswith('/test') else body('private-alice')
    for token in (None, 'wrong-token'):
        status, _, raw = request(wire, path, payload, token=token)
        value = json.loads(raw)
        assert status == 401 and value['error']['type'] == 'authentication_error', raw
        assert set(value['error']) == {'message', 'type', 'code', 'param'}
    status, _, raw = request(wire, path, payload, token='bearer', extra=[('X-Gateway-Token', 'conflict')])
    assert status == 401 and json.loads(raw)['error']['type'] == 'authentication_error'
    status, _, raw = request(wire, path, payload, token='bearer', extra=[('Authorization', 'Bearer wrong')])
    assert status == 401
    assert not wire[2]
    assert request(wire, path, payload, token='gateway')[0] == 200


def test_bearer_does_not_expand_existing_client_business_protocol(wire):
    status, _, raw = request(wire, '/client/business',
        {'action': 'llm_records.list', 'request_id': 'read', 'params': {}})
    assert status == 403 and json.loads(raw)['error'] == 'forbidden'
    assert not wire[2]


def test_disabled_switch_http_404_business_503_and_capability_readiness(wire):
    wire[1].config.openai_compat_gateway_enabled = False
    for path, payload in (('/v1/chat/completions', body('private-alice')), ('/api/llm/test', {})):
        status, _, raw = request(wire, path, payload)
        assert status == 404 and json.loads(raw)['error']['type'] == 'not_found_error'
    status, _, value = business(wire, 'llm.gateway.test')
    assert status == 503 and value['error']['code'] == 'HOST_NOT_READY', value
    status, _, value = business(wire, 'system.capabilities.extensions')
    assert status == 200
    assert 'llm.gateway.test' not in value['data']['supported_actions']
    assert not wire[2] and not wire[3]


def test_unknown_invalid_request_payload_limit_and_rate_limit(wire):
    status, _, raw = request(wire, '/v1/chat/completions', body('unknown'))
    assert status == 404 and json.loads(raw)['error']['code'] == 'RESOURCE_NOT_FOUND'
    for extra in ({'n': 2, 'stream': True}, {'tools': [], 'stream': True}, {'functions': [], 'stream': True}):
        status, _, raw = request(wire, '/v1/chat/completions', body('private-alice', **extra))
        assert status == 400 and json.loads(raw)['error']['type'] == 'invalid_request_error'
    status, _, raw = request(wire, '/api/llm/test', {}, raw=b'{')
    assert status == 400 and json.loads(raw)['error']['type'] == 'invalid_request_error'
    # 声明超限不需要真的分配超限正文，服务必须在读正文前拒绝。
    assert REQUEST_MAX_BYTES > 0
    server = wire[0]
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=10)
    try:
        connection.request('POST', '/api/llm/test', b'{}', {'Authorization': 'Bearer ' + server._local_client_credential,
            'Content-Type': 'application/json', 'Content-Length': str(REQUEST_MAX_BYTES + 1)})
        response = connection.getresponse()
        assert response.status == 413 and json.loads(response.read())['error']['type'] == 'invalid_request_error'
    finally:
        connection.close()
    limits = request_worker.AdmissionLimits.from_config(wire[1].config)
    assert request_worker.admission.try_acquire('alice', limits.user_inflight, limits.global_inflight)
    try:
        status, _, raw = request(wire, '/v1/chat/completions', body('private-alice'))
        assert status == 429 and json.loads(raw)['error']['type'] == 'rate_limit_error', raw
    finally:
        request_worker.admission.release('alice')
    assert not wire[2]


def test_real_authenticated_owner_isolation_records_profiles_and_delete(wire):
    assert request(wire, '/v1/chat/completions', body('private-bob'), user='bob')[0] == 200
    status, _, envelope = business(wire, 'llm_records.list', user='bob')
    assert status == 200
    foreign_record = envelope['data']['items'][0]['record_id']
    bob_profile = next(profile for host, profile in wire[3].values() if host.home_paths.owner_id.endswith('/bob'))
    status, _, raw = request(wire, '/v1/chat/completions', body(bob_profile), user='alice')
    assert status == 404 and json.loads(raw)['error']['code'] == 'RESOURCE_NOT_FOUND'
    assert business(wire, 'llm_records.get', {'record_id': foreign_record})[0] == 404
    status, _, envelope = business(wire, 'llm_records.delete', {'record_id': foreign_record})
    assert status == 200 and envelope['data']['deleted'] is False, envelope
    assert business(wire, 'llm_records.get', {'record_id': foreign_record}, user='bob')[0] == 200
    status, _, envelope = business(wire, 'llm_records.list')
    assert status == 200 and envelope['data']['items'] == []
