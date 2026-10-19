"""真实临时C/凭据/HTTP pool与等待耐久；只连本测试127.0.0.1，不调用模型或日常服务。"""
from __future__ import annotations

import http.client
import json
import time
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.gateway_parts import http_handlers, wait_acceptance
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_for_root
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case


# LLM: 依赖均由原临时C/lease/真实Agent准备；复用curator_case已retain的唯一原Root，不能另建Root重绑gate；最后关自有loopback设施。
# 函数用途: 创建与原C同paths/control/runtime的测试监听器，所有持久资料仍在pytest临时home。
@contextmanager
def c_http(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (agent, service, backend, gate, claim, close):
        control = coordinator_for_root(gate.paths.root, context=gate.paths).control_plane
        runtime = gate._ordinary_admission_origin[0]
        assert runtime.control is control
        server = GatewayHTTPServer(0, gate.paths, params=GatewayHTTPServerParams(control_plane=control, drain_runtime=runtime))
        server.start()
        listener = server._thread
        try:
            yield agent, backend, gate, control, runtime, server, close
        finally:
            if control.controller() is not None and control.controller().active():
                runtime.stop_producers()
            server.stop()
            assert not listener.is_alive()


# LLM: 仅发给本fixture的真实回环端口；凭据取本次原G1内存值不打印、不复用真实配置，连接每次明确关闭。
# 函数用途: 通过完整dispatcher与原middleware请求临时HTTP，返回已读JSON。
def request(server, method, path, body=None, *, credential=True, headers=None):
    supplied = {'X-User-Id': 'alice', 'X-Channel': 'feishu'}
    if credential:
        supplied['X-Gateway-Token'] = server._local_client_credential
    supplied.update(headers or {})
    payload = None if body is None else json.dumps(body).encode()
    if payload is not None:
        supplied['Content-Type'] = 'application/json'
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=4)
    try:
        connection.request(method, path, payload, supplied)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


# LLM: 只增加隔离这一拒绝事实，不伪造许可/claim或启动旧树观察；准备与提交仍原control、同lease和真实文件writer。
# 函数用途: 把本次临时C限制为隔离接收，随后检查实际耐久成功边界。
def isolate(control):
    controller = control.controller()
    with controller._mutex:
        controller._all_quarantined = True


def body(*, idempotent=True):
    metadata = {'channel_chat_type': 'private'}
    if idempotent:
        metadata['message_id'] = 'synthetic-http-message'
    return {'goal': '读取已有材料后说明结果', 'conversation_id': 'synthetic-http-private', 'metadata': metadata}


def test_c_only_listener_keeps_main_auth_and_actual_original_pool_workers(tmp_path, monkeypatch):
    with c_http(tmp_path, monkeypatch) as (_agent, backend, gate, control, runtime, server, _close):
        assert server.agent is None and server.auth_middleware is not None
        before = set(gate.paths.inbox.glob('*.json'))
        code, rejected = request(server, 'POST', '/ask', body(), credential=False)
        assert code == 403 and rejected['error_code'] == 'LOCAL_CREDENTIAL_REQUIRED'
        assert set(gate.paths.inbox.glob('*.json')) == before
        code, _ = request(server, 'GET', '/status')
        assert code == 200
        code, unavailable = request(server, 'POST', '/control', {}, headers={'X-My-Agent-Control': 'global-write-v1'})
        assert code == 503 and unavailable == {'error_code': 'GLOBAL_WRITE_SERVICE_UNAVAILABLE'}
        server.agent = _agent
        try:
            code, ordinary = request(server, 'GET', '/control-status/gwctl-msg-' + '0' * 32)
            assert code == 404 and ordinary == {'error': 'control operation not found'}
        finally:
            server.agent = None
        group = runtime._resources['http/requests']
        assert group.owner is server.server._request_executor and group._born_threads
        assert runtime._resources['http/listener'].owner is server._thread
        assert backend.calls == 0
    assert all(group.observe(time.monotonic() + 2) == 'settled' for group in runtime._resources.values())


@pytest.mark.parametrize('idempotent', [False, True])
def test_wait_202_syncs_real_queue_receipt_and_never_materializes_owner(tmp_path, monkeypatch, idempotent):
    synced, prompted = [], []
    sync = wait_acceptance._sync_existing_control_file

    def observe_sync(root, path):
        sync(root, path)
        synced.append(path)

    monkeypatch.setattr(wait_acceptance, '_sync_existing_control_file', observe_sync)
    monkeypatch.setattr(http_handlers, 'prompt_queued', lambda *_: prompted.append(True))
    with c_http(tmp_path, monkeypatch) as (_agent, backend, gate, control, _runtime, server, _close):
        isolate(control)
        owner_home = resolve_owner_home(control.home_root, OwnerIdentity.provider_user('feishu', 'alice')).home_dir
        assert not owner_home.exists()
        code, accepted = request(server, 'POST', '/ask', body(idempotent=idempotent))
        assert code == 202 and accepted['terminal'] is False
        assert accepted['error_code'] == 'OWNER_PARTITION_QUARANTINED'
        request_id = accepted['request_id']
        queue = gate.paths.inbox / (request_id + '.json')
        assert queue in synced and read_json_file(queue)['id'] == request_id
        assert not owner_home.exists() and prompted == [] and backend.calls == 0
        result_code, result = request(server, 'GET', '/result/' + request_id)
        assert result_code == 202 and result['error_code'] == 'OWNER_PARTITION_QUARANTINED'
        forbidden_code, forbidden = request(server, 'GET', '/result/' + request_id, headers={'X-User-Id': 'bob'})
        assert forbidden_code == 403 and 'reason' not in forbidden
        if idempotent:
            receipt = read_gateway_input_receipt(gate.paths, request_id)
            original = receipt.prepared_request
            assert receipt.state == 'queued' and len(synced) == 2

            def forbidden(*_args, **_kwargs):
                pytest.fail('replayed receipt must not rerun first source preparation')

            monkeypatch.setattr(http_handlers, '_prepare_http_gateway_input', forbidden)
            again, replay = request(server, 'POST', '/ask', body())
            assert again == 202 and replay['request_id'] == request_id
            assert read_gateway_input_receipt(gate.paths, request_id).prepared_request == original
            status, projected = request(server, 'GET', '/input-status/' + request_id)
            assert status == 202 and projected['error_code'] == 'OWNER_PARTITION_QUARANTINED'
        else:
            assert read_gateway_input_receipt(gate.paths, request_id) is None and len(synced) == 1


@pytest.mark.parametrize('damage', ['empty', 'wrong_id', 'wrong_user', 'sync_error',
                                  'symlink', 'missing_queue', 'corrupt_terminal', 'corrupt_receipt'])
def test_corrupt_or_unsynced_original_input_cannot_claim_wait_202(tmp_path, monkeypatch, damage):
    confirm = wait_acceptance.confirm_wait_acceptance

    def damaged(paths, request_id, wait, *, receipt=None):
        path = paths.inbox / (request_id + '.json')
        if damage == 'sync_error':
            raise OSError('synthetic sensitive sync detail')
        if damage == 'symlink':
            target = tmp_path / 'synthetic-external.json'
            target.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(target)
        elif damage == 'missing_queue':
            path.unlink()
        elif damage == 'corrupt_terminal':
            (paths.terminal / path.name).write_text('{}')
        elif damage == 'corrupt_receipt':
            wait_acceptance.gateway_input_receipt_path(paths, request_id).write_bytes(b'')
        elif damage == 'empty':
            path.write_bytes(b'')
        else:
            value = read_json_file(path)
            value['id' if damage == 'wrong_id' else 'user_id'] = 'not-original'
            path.write_text(json.dumps(value))
        return confirm(paths, request_id, wait, receipt=receipt)

    monkeypatch.setattr(wait_acceptance, 'confirm_wait_acceptance', damaged)
    with c_http(tmp_path, monkeypatch) as (_agent, backend, gate, control, _runtime, server, _close):
        isolate(control)
        code, rejected = request(server, 'POST', '/ask', body())
        assert code == 503 and rejected['terminal'] is False and rejected['request_id']
        assert 'synthetic sensitive' not in json.dumps(rejected)
        assert backend.calls == 0
        assert not list(gate.paths.terminal.glob('*.json')) or damage == 'corrupt_terminal'


def test_real_source_closes_during_first_prepare_without_queue_commit(tmp_path, monkeypatch):
    original = http_handlers._prepare_http_gateway_input
    with c_http(tmp_path, monkeypatch) as (_agent, backend, gate, control, _runtime, server, close):
        before = set(gate.paths.inbox.glob('*.json'))

        def close_before_commit(*args):
            prepared = original(*args)
            close()
            return prepared

        monkeypatch.setattr(http_handlers, '_prepare_http_gateway_input', close_before_commit)
        code, rejected = request(server, 'POST', '/ask', body())
        assert code != 202 and rejected
        assert set(gate.paths.inbox.glob('*.json')) == before and backend.calls == 0


@pytest.mark.parametrize('case', [
    ('POST', '/control', {'command': '/status', 'conversation_id': 'synthetic-http-private'}, 'identity-required'),
    ('POST', '/client/history', {'conversation_id': 'synthetic-http-private'}, 'history'),
    ('GET', '/control-status/gwctl-msg-' + '0' * 32, None, 'absent'),
    ('GET', '/control-status/not-present', None, 'invalid'),
    ('POST', '/ask', {'goal': '/status', 'conversation_id': 'synthetic-http-private'}, 'identity-required'),
])
def test_c_business_wait_follows_original_credential_gate(tmp_path, monkeypatch, case):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import request_worker
    from agent_py_agent.tests.test_gateway_coordinator_client_history import snapshot

    method, path, payload, expected = case
    with c_http(tmp_path, monkeypatch) as (_agent, backend, gate, control, _runtime, server, _close):
        queued = set(gate.paths.inbox.glob('*.json'))
        before = snapshot(control.home_root)
        before_operations = {p.name: p.read_bytes() for p in (gate.paths.root/'control_operations').glob('*.json')}

        def forbidden(*_args, **_kwargs):
            pytest.fail('冷C的认证、等待与只读查询不能构造业务Agent或foreign pool')

        monkeypatch.setattr(SimpleAgent, '__init__', forbidden)
        monkeypatch.setattr(request_worker, '_owner_pool', forbidden)
        denied, denial = request(server, method, path, payload, credential=False)
        assert denied == 403 and denial['error_code'] == 'LOCAL_CREDENTIAL_REQUIRED'
        assert snapshot(control.home_root) == before
        status, result = request(server, method, path, payload)
        if expected == 'waiting':
            assert status == 503 and result['status'] == 'waiting' and result['terminal'] is False
            assert result['error_code'] == 'COORDINATOR_BUSINESS_NOT_READY'
        elif expected == 'identity-required':
            assert status == 400 and result == {'error': 'control command requires metadata.message_id',
                                               'error_code': 'CONTROL_OPERATION_ID_REQUIRED'}
        elif expected == 'history':
            assert status == 200 and result == {
                'ok': True, 'thread_id': '', 'turns': [], 'load_errors': [], 'display_events': [],
                'message_cursor': 0, 'before_message_cursor': 0, 'has_older': False,
            }
        elif expected == 'absent':
            assert status == 404 and result == {'error': 'control operation not found'}
        else:
            assert expected == 'invalid' and status == 503
            assert result == {'error': 'gateway control operation id is invalid',
                              'error_code': 'CONTROL_OPERATION_RECEIPT_CORRUPT'}
        assert set(gate.paths.inbox.glob('*.json')) == queued and backend.calls == 0
        assert snapshot(control.home_root) == before
        assert {p.name: p.read_bytes() for p in (gate.paths.root/'control_operations').glob('*.json')} == before_operations


def test_count_zero_without_agent_keeps_original_control_none_contract(tmp_path):
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / 'gateway'))
    server.start()
    try:
        status, missing = request(server, 'POST', '/control', {'command': '/status', 'conversation_id': 'test'})
        assert status == 500 and missing == {'error': 'server control service not initialized'}
    finally:
        server.stop()
