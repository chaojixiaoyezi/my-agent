# LLM: scale-s7 回归（scale-s1残留）：历史读口（/result、/progress ACL、POST等待确认、CLI result）经封存索引回读同一终态；
#   轮询热路径（默认读口、本机等待循环、在途/result与ACL）不打开封存索引；句柄换出后按持久化锚点推进到底。
# 模块用途: 用真实终态builder、封存入口与HTTP服务验证读口切换和封存进度。
from __future__ import annotations

import http.client
import json
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import gateway_paths
from agent_py_agent.agent.gateway_parts import owner_retention as archive
from agent_py_agent.agent.gateway_parts.http_handlers import (
    _can_read_finished_request,
    _FinishedRequestAccess,
    _ResultAccessContext,
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
    read_gateway_terminal_response_file,
)
from agent_py_agent.agent.gateway_parts.wait_acceptance import confirm_wait_acceptance
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli import gateway_client


# 函数用途: 用原产品builder准备一个已结算、可封存的终态（同test_gateway_receipt_archive._settled）。
def _settled(paths, key='request-a', *, completed_at=1):
    response = {'id': key, 'ok': True, 'status': 'done', 'ended_at': completed_at, 'output': 'answer'}
    payload = _terminal_gateway_request_payload(key, {'id': key, 'user_id': 'alice', 'prompt': 'hello'}, response)
    write_json_file_atomic(paths.terminal / f'{key}.json', payload)
    write_json_file_atomic(paths.done / f'{key}.json', payload)
    write_json_file_atomic(paths.responses / f'{key}.json', response)
    mark_gateway_terminal_projection_complete(paths, key, response, settlement_pending=False)
    return payload, response


def _seal(paths):
    report = archive.seal_gateway_receipts_slice(paths, before=100, max_entries=64, max_ms=1000)
    assert report['errors'] == []
    return report


def _no_index(*_args, **_kwargs):
    raise AssertionError('hot path must not open the receipt index')


def test_default_owner_maintenance_retires_originals_and_readers_follow(tmp_path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due
    from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent

    home = ensure_my_agent_home(tmp_path / 'home')
    paths = gateway_paths_from_root(runtime_paths_for_agent(None, home.owner_home_dir, home)['gateway_workspace'])
    payload, _ = _settled(paths)
    assert run_owner_retention_if_due(home, now=2_000_000, include_global_indexes=False).ran
    assert not (paths.terminal / 'request-a.json').exists()
    assert archive.gateway_terminal_exists(paths, 'request-a.json')
    sealed = read_gateway_terminal_envelope_report(paths.terminal / 'request-a.json', include_sealed=True)
    assert sealed.load_error is None and sealed.payload == payload


def test_envelope_reader_default_keeps_polling_semantics(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    payload, response = _settled(paths)
    _seal(paths)
    terminal = paths.terminal / 'request-a.json'
    assert read_gateway_terminal_response_file(terminal, include_sealed=True) == response
    assert read_gateway_terminal_envelope_report(paths.terminal / 'unknown.json', include_sealed=True).payload == {}
    monkeypatch.setattr(archive, '_receipt_index', _no_index)
    assert read_gateway_terminal_envelope_report(terminal).payload == {}
    assert read_gateway_terminal_response_file(terminal) == {}


def test_progress_acl_and_wait_confirmation_see_sealed_request(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _settled(paths)
    _seal(paths)
    user = SimpleNamespace(can_access_all_users=False)
    assert _can_read_finished_request(paths, _ResultAccessContext('request-a', 'alice', user)) is _FinishedRequestAccess.ALLOWED
    assert _can_read_finished_request(paths, _ResultAccessContext('request-a', 'bob', user)) is _FinishedRequestAccess.DENIED
    assert _can_read_finished_request(paths, _ResultAccessContext('missing', 'alice', user)) is _FinishedRequestAccess.NOT_FOUND
    before = sorted(str(path) for path in paths.root.rglob('*'))
    confirm_wait_acceptance(paths, 'request-a', {})
    assert sorted(str(path) for path in paths.root.rglob('*')) == before
    with pytest.raises(FileNotFoundError):
        confirm_wait_acceptance(paths, 'missing', {})


def test_http_result_serves_sealed_request_and_inflight_poll_skips_index(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _settled(paths)
    _seal(paths)
    write_json_file_atomic(paths.processing / 'inflight.json', {'id': 'inflight', 'user_id': 'alice', 'prompt': 'hi'})
    root = tmp_path / 'data-root'
    credential = ensure_local_client_credential(root)
    middleware = AuthMiddleware(AuthManager(admin_user_id='admin', auth_enabled=True))
    middleware.set_local_client_credential(credential)
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=root, owner_home_dir=root / 'owners' / 'local' / 'main'))
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent, auth_middleware=middleware))
    server.start()
    try:
        port = server.server.server_address[1]
        status, body = _get(port, '/result/request-a', credential)
        assert status == 200 and body['status'] == 'done' and 'answer' in json.dumps(body), body
        assert not (paths.responses / 'request-a.json').exists() and not (paths.terminal / 'request-a.json').exists()
        monkeypatch.setattr(archive, '_receipt_index', _no_index)
        status, body = _get(port, '/result/inflight', credential)
        assert status == 202, body
    finally:
        server.stop()


def test_local_wait_loop_and_inflight_acl_never_open_the_index(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.request_worker import wait_for_gateway_response

    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _settled(paths)
    _seal(paths)
    write_json_file_atomic(paths.processing / 'inflight.json', {'id': 'inflight', 'user_id': 'alice', 'prompt': 'hi'})
    monkeypatch.setattr(archive, '_receipt_index', _no_index)
    assert wait_for_gateway_response(paths, 'inflight', 0.05) == {}
    user = SimpleNamespace(can_access_all_users=False)
    assert _can_read_finished_request(paths, _ResultAccessContext('inflight', 'alice', user)) is _FinishedRequestAccess.ALLOWED
    assert _can_read_finished_request(paths, _ResultAccessContext('inflight', 'bob', user)) is _FinishedRequestAccess.DENIED


def test_prepared_input_without_target_never_opens_the_index(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.queued_steer_expiry import (
        queued_steer_expiry_response,
        queued_steer_pre_acquire,
    )
    from agent_py_agent.tests.test_scale_s1_gateway_loose_retirement import _prepared_input

    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _settled(paths)
    _seal(paths)
    receipt = _prepared_input(paths, 'ordinary')
    assert 'expected_turn_id' not in receipt.prepared_request['metadata']
    monkeypatch.setattr(archive, '_receipt_index', _no_index)
    for _ in range(3):
        assert queued_steer_expiry_response(paths, receipt.prepared_request) is None
        assert queued_steer_pre_acquire(paths, receipt.prepared_request) == ''
        assert read_gateway_terminal_envelope_report(paths.terminal / f'{receipt.request_id}.json').payload == {}
        assert read_gateway_terminal_response_file(paths.terminal / f'{receipt.request_id}.json') == {}


def test_cli_gateway_result_reads_sealed_request(tmp_path, monkeypatch, capsys):
    agent = SimpleAgent(AgentConfig(model_backend='echo', gateway_workspace='gateway',
                                    local_store_path='local_store/local.db', local_store_files_dir='local_store/files',
                                    local_store_events_path='local_store/events.jsonl'), tmp_path)
    paths = gateway_paths(agent)
    _settled(paths)
    _seal(paths)
    monkeypatch.setattr(gateway_client, 'make_agent', lambda _args: agent)
    code = gateway_client.cmd_gateway_result(SimpleNamespace(request_id='request-a', json=True, show_prompt=False))
    assert code == 0 and 'answer' in capsys.readouterr().out


# 函数用途: 按名字排序枚举，让不可封存的年轻终态固定排在目录头部（复现换出后从头重扫的卡死）。
def _sorted_scan(directory):
    with os.scandir(directory) as entries:
        rows = sorted(entries, key=lambda entry: entry.name)
    yield from rows


def test_sealing_resumes_past_unsealable_head_after_iterator_eviction(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, '_receipt_scan_entries', _sorted_scan)
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    for index in range(12):
        _settled(paths, f'a-young-{index:02d}', completed_at=10_000)
        _settled(paths, f'b-old-{index:02d}')
    anchor, reports = '', []
    for _ in range(40):
        archive._RECEIPT_SCANS.clear()
        report = archive.seal_gateway_receipts_slice(paths, before=100, max_entries=4, max_ms=1000, resume_after=anchor)
        reports.append(report)
        anchor = report['resume_after']
        if report['complete']:
            break
    assert reports[-1]['complete'] and reports[-1]['resume_after'] == ''
    assert all(row['scanned_entries'] <= 4 and not row['errors'] for row in reports)
    assert sum(row['sealed_requests'] for row in reports) == 12
    assert sorted(path.name for path in paths.terminal.glob('*.json')) == [f'a-young-{index:02d}.json' for index in range(12)]
    assert all(archive.gateway_terminal_exists(paths, f'b-old-{index:02d}.json') for index in range(12))


def test_vanished_anchor_restarts_from_head_without_claiming_complete(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _settled(paths, 'young', completed_at=10_000)
    archive._RECEIPT_SCANS.clear()
    report = archive.seal_gateway_receipts_slice(paths, before=100, max_entries=4, max_ms=1000, resume_after='gone.json')
    assert not report['complete'] and report['resume_after'] == '' and report['scanned_entries'] == 0
    report = archive.seal_gateway_receipts_slice(paths, before=100, max_entries=4, max_ms=1000, resume_after='')
    assert report['complete'] and report['scanned_entries'] == 1 and (paths.terminal / 'young.json').exists()


def test_owner_lane_cursor_persists_the_resume_anchor(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due
    from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent

    seen = []
    original = archive.seal_gateway_receipts_slice

    def recording(*args, **kwargs):
        seen.append(kwargs.get('resume_after'))
        archive._RECEIPT_SCANS.clear()
        return original(*args, **{**kwargs, 'max_entries': 1})

    monkeypatch.setattr(archive, 'seal_gateway_receipts_slice', recording)
    home = ensure_my_agent_home(tmp_path / 'home')
    paths = gateway_paths_from_root(runtime_paths_for_agent(None, home.owner_home_dir, home)['gateway_workspace'])
    for index in range(3):
        _settled(paths, f'young-{index}', completed_at=10_000_000)
    for tick in range(6):
        run_owner_retention_if_due(home, now=2_000_000 + tick, include_global_indexes=False)
    cursor = json.loads((home.owner_data_dir / 'maintenance.json').read_text())['storage_maintenance']['cursors']
    assert seen[0] == '' and any(seen[1:]), seen
    assert 'resume_after' in cursor['gateway_terminal']


# 函数用途: 带本机凭据发一次本机GET并解析JSON。
def _get(port, path, credential):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        connection.request('GET', path, headers={'X-Gateway-Token': credential})
        response = connection.getresponse()
        data = response.read()
        return response.status, json.loads(data) if data else {}
    finally:
        connection.close()
