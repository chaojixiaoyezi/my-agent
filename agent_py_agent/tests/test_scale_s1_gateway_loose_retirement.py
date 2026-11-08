# LLM: 仅用临时Gateway和真实终态builder；封存后原ACL、终态校验及等待耐久合同保持。
# 模块用途: 验证生产封存删除原件后的回读、HTTP结果、权限与扫描换出进度。
from __future__ import annotations

import http.client
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.gateway_parts import owner_retention as archive
from agent_py_agent.agent.gateway_parts.http_handlers import (
    _can_read_finished_request,
    _FinishedRequestAccess,
    _ResultAccessContext,
    _send_archived_terminal_result,
)
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.local_client_token import ensure_local_client_credential
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.recovery import (
    _terminal_gateway_request_payload,
    mark_gateway_terminal_projection_complete,
)
from agent_py_agent.agent.gateway_parts.response_renderer import (
    read_gateway_terminal_envelope_report,
)
from agent_py_agent.agent.gateway_parts.wait_acceptance import confirm_wait_acceptance


def _settled(paths, key='request-a', *, completed_at=1, pending=False):
    """复制原归档测试的真实builder夹具，避免跨测试文件共享装配。"""
    response = {'id': key, 'ok': True, 'status': 'done', 'ended_at': completed_at, 'output': 'answer'}
    payload = _terminal_gateway_request_payload(key, {'id': key, 'user_id': 'alice', 'prompt': 'hello'}, response)
    write_json_file_atomic(paths.terminal / f'{key}.json', payload)
    write_json_file_atomic(paths.done / f'{key}.json', payload)
    write_json_file_atomic(paths.responses / f'{key}.json', response)
    mark_gateway_terminal_projection_complete(paths, key, response, settlement_pending=pending)
    chunk = paths.done / f'{key}.chunks.jsonl'
    chunk.write_bytes(b'{"event":"delta","text":"answer"}\n')
    return payload, response, chunk


def _seal(paths):
    report = archive.seal_gateway_receipts_slice(paths, before=100, max_ms=1000)
    assert report['errors'] == [] and report['sealed_requests'] == 1


@pytest.mark.parametrize('hold', [False, True])
def test_owner_maintenance_removes_sealed_originals_unless_legal_hold(tmp_path, hold):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due
    from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent

    home = ensure_my_agent_home(tmp_path / 'home')
    paths = gateway_paths_from_root(runtime_paths_for_agent(None, home.owner_home_dir, home)['gateway_workspace'])
    _settled(paths)
    policy = json.loads(home.owner_retention_json.read_text())
    policy['legal_hold'] = hold
    home.owner_retention_json.write_text(json.dumps(policy))
    assert run_owner_retention_if_due(home, now=2_000_000, include_global_indexes=False).ran
    assert (paths.terminal / 'request-a.json').exists() is hold
    assert (paths.responses / 'request-a.json').exists() is hold
    assert archive.gateway_terminal_exists(paths, 'request-a.json')


def test_envelope_reader_falls_back_to_sealed_copy(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _settled(paths)
    path = paths.terminal / 'request-a.json'
    before = read_gateway_terminal_envelope_report(path)
    assert before.load_error is None and before.payload
    _seal(paths)
    after = read_gateway_terminal_envelope_report(path)
    assert after.payload == before.payload and after.load_error is None
    missing = read_gateway_terminal_envelope_report(paths.terminal / 'unknown.json')
    assert missing.payload == {} and missing.load_error is None


@pytest.mark.parametrize('repair', [False, True])
def test_result_serves_sealed_request_without_recreating_loose_files(tmp_path, repair):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _, response, _ = _settled(paths)
    _seal(paths)
    replies = []
    handler = SimpleNamespace(_send_json=lambda code, body: replies.append((code, body)))
    assert _send_archived_terminal_result(handler, SimpleNamespace(paths=paths),
        _ResultAccessContext('request-a', 'alice', None), repair_response_projection=repair)
    assert replies == [(200, response)]
    assert not (paths.responses / 'request-a.json').exists()
    assert not (paths.terminal / 'request-a.json').exists()


@pytest.mark.parametrize('request_id,user,expected', [
    ('request-a', 'alice', _FinishedRequestAccess.ALLOWED),
    ('request-a', 'bob', _FinishedRequestAccess.DENIED),
    ('unknown', 'alice', _FinishedRequestAccess.NOT_FOUND),
])
def test_progress_acl_sees_sealed_request(tmp_path, request_id, user, expected):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _settled(paths)
    _seal(paths)
    permission = SimpleNamespace(can_access_all_users=False)
    assert _can_read_finished_request(paths, _ResultAccessContext(request_id, user, permission)) is expected


def test_confirm_wait_acceptance_accepts_sealed_terminal(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _settled(paths)
    _seal(paths)
    requests = paths.terminal.parent
    before = {path.relative_to(requests): path.read_bytes() for path in requests.rglob('*') if path.is_file()}
    confirm_wait_acceptance(paths, 'request-a', {})
    after = {path.relative_to(requests): path.read_bytes() for path in requests.rglob('*') if path.is_file()}
    assert after == before
    assert not (paths.terminal / 'request-a.json').exists()


def test_sealing_progresses_when_scan_iterator_is_evicted(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    for index in range(40):
        _settled(paths, f'request-{index}')
    for _ in range(40):
        report = archive.seal_gateway_receipts_slice(paths, before=100, max_entries=4, max_ms=1000)
        archive._RECEIPT_SCANS.clear()
        assert report['errors'] == [] and report['scanned_entries'] <= 4
        if not any(paths.terminal.glob('*.json')):
            break
    assert not list(paths.terminal.glob('*.json'))
    assert all(archive.gateway_terminal_exists(paths, f'request-{index}.json') for index in range(40))


def test_http_client_roundtrip_reads_sealed_result_without_repair(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _, response, _ = _settled(paths)
    _seal(paths)
    root = tmp_path / 'data-root'
    credential = ensure_local_client_credential(root)
    middleware = AuthMiddleware(AuthManager(admin_user_id='admin', auth_enabled=True))
    middleware.set_local_client_credential(credential)
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=root, owner_home_dir=root / 'owners' / 'local' / 'main'))
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent, auth_middleware=middleware))
    server.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=5)
    try:
        connection.request('GET', '/result/request-a', headers={
            'X-Gateway-Token': credential, 'X-User-Id': 'admin', 'X-Channel': 'web'})
        result = connection.getresponse()
        assert result.status == 200 and json.loads(result.read()) == response
        assert not (paths.responses / 'request-a.json').exists()
        assert not (paths.terminal / 'request-a.json').exists()
    finally:
        connection.close()
        server.stop()
