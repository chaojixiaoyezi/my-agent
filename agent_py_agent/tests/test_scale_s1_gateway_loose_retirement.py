# LLM: 仅用临时Gateway和真实终态builder；封存后原ACL、终态校验及等待耐久合同保持。
# 模块用途: 验证生产封存删除原件后的回读、HTTP结果、权限与扫描换出进度。
from __future__ import annotations

import http.client
import json
import os
import sys
from functools import partial
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import owner_retention as archive
from agent_py_agent.agent.gateway_parts import queued_steer_expiry as expiry
from agent_py_agent.agent.gateway_parts import request_worker as worker
from agent_py_agent.agent.gateway_parts.http_handlers import (
    _AskRequestContext,
    _build_ask_request,
    _can_read_finished_request,
    _FinishedRequestAccess,
    _http_idempotent_request_identity,
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
from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.recovery import (
    _project_recovered_terminal_response,
    _terminal_gateway_request_payload,
    mark_gateway_terminal_projection_complete,
    terminalize_gateway_request_file,
)
from agent_py_agent.agent.gateway_parts.request_owner_binding import bind_request_owner
from agent_py_agent.agent.gateway_parts.response_renderer import (
    read_gateway_terminal_envelope_report,
)
from agent_py_agent.agent.gateway_parts.wait_acceptance import confirm_wait_acceptance
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig


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
    assert read_gateway_terminal_envelope_report(path).payload == {}
    after = read_gateway_terminal_envelope_report(path, include_sealed=True)
    assert after.payload == before.payload and after.load_error is None
    missing = read_gateway_terminal_envelope_report(paths.terminal / 'unknown.json', include_sealed=True)
    assert missing.payload == {} and missing.load_error is None


@pytest.mark.parametrize('repair', [False, True])
def test_result_serves_sealed_request_without_recreating_loose_files(tmp_path, repair):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    _, response, _ = _settled(paths)
    _seal(paths)
    replies = []
    handler = SimpleNamespace(_send_json=lambda code, body: replies.append((code, body)))
    assert _send_archived_terminal_result(handler, SimpleNamespace(paths=paths),
        _ResultAccessContext('request-a', 'alice', None), repair_response_projection=repair, include_sealed=True)
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


def _prepared_input(paths, message_id, *, target='', user='alice', conversation='thread'):
    """沿原HTTP身份/builder与Count0准备入口入队，不手造owner、指纹或回执。"""
    ensure_gateway_folders(paths)
    goal = '补充输入'
    body = {'conversation_id': conversation, 'metadata': {'message_id': message_id}}
    if target:
        body['metadata']['expected_turn_id'] = target
    request_id, digest = _http_idempotent_request_identity(body, goal=goal, user_id=user, channel='web')
    body['metadata']['client_input_digest'] = digest
    payload = _build_ask_request(_AskRequestContext(body, goal, request_id, user, 'web'))
    host = SimpleNamespace(config=AgentConfig(gateway_per_user_owner_scoping=True),
                           home_paths=SimpleNamespace(root=paths.root.parent / 'home'))
    with inputs.gateway_input_transition(paths, request_id):
        receipt, created = inputs.load_or_prepare_gateway_input_locked(
            paths, request_id=request_id, client_input_digest=digest, client_message_id=message_id,
            guidance_dedupe_key='', prepared_request=payload,
            prepare_request=partial(bind_request_owner, host))
        assert created
        receipt, queued = inputs.queue_gateway_input_locked(paths, receipt)
        assert queued
    return receipt


def _finish_input(paths, receipt, status):
    """真实claim/终态提交构造可封存目标；不运行模型。"""
    claim = worker.claim_request(paths, paths.inbox / f'{receipt.request_id}.json')
    assert claim is not None
    _finish_claim(paths, claim, status)
    return read_gateway_terminal_envelope_report(paths.terminal / claim.name).payload


def _finish_claim(paths, claim, status):
    response = {'id': claim.request_id, 'ok': status == 'done', 'status': status, 'ended_at': 1, 'response': ''}
    terminalize_gateway_request_file(paths, claim.path, paths.done if status == 'done' else paths.failed,
                                    claim.request_id, terminal_response=response)
    _project_recovered_terminal_response(paths, claim.request_id, True)


def _live_fds():
    """只比较实际存活描述符；listdir自己的临时目录句柄已关闭。"""
    directory = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'
    if not os.path.isdir(directory):
        return None
    result = set()
    for name in os.listdir(directory):
        descriptor = int(name)
        try:
            info = os.fstat(descriptor)
        except OSError:
            continue
        result.add((descriptor, info.st_dev, info.st_ino, info.st_mode))
    return result


@pytest.fixture
def steer_dispatch(tmp_path, monkeypatch):
    """记录真实认领和提交；同步执行替身仍用原终态写端，并在finally归还原准入槽。"""
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    admission = worker.GatewayAdmission()
    monkeypatch.setattr(worker, 'admission', admission)
    claims, submissions, active_counts = [], [], []
    claim_request = worker.claim_request

    def claim(*args, **kwargs):
        result = claim_request(*args, **kwargs)
        if result is not None:
            claims.append(result.request_id)
        return result

    def submit(claim, user_key, conversation_key):
        submissions.append(claim.request_id)
        active_counts.append(admission.snapshot()['total'])
        try:
            _finish_claim(paths, claim, 'done')
        finally:
            admission.release(user_key, conversation_key=conversation_key)

    # 目标构造也使用原claim，但只统计实际dispatcher发起的认领。
    state = SimpleNamespace(paths=paths, claims=claims, submissions=submissions, active_counts=active_counts,
                            dispatch=lambda: worker.dispatch_pending_requests(paths, worker.AdmissionLimits(1, 1), submit),
                            observe=lambda: monkeypatch.setattr(worker, 'claim_request', claim))
    before = _live_fds()
    yield state
    assert admission.snapshot()['total'] == 0
    assert all(count <= 1 for count in active_counts)
    after = _live_fds()
    if before is not None:
        assert after == before, ('fd_delta', after - before, before - after)


@pytest.mark.parametrize('status,sealed', [('cancelled', False), ('cancelled', True),
                                        ('stopped', False), ('interrupted', False)])
def test_stopped_target_expires_prepared_steer_before_claim(steer_dispatch, sealed, status):
    state = steer_dispatch
    target = _prepared_input(state.paths, 'target')
    canonical = _finish_input(state.paths, target, status)
    if sealed:
        _seal(state.paths)
        assert not (state.paths.terminal / f'{target.request_id}.json').exists()
    pending = _prepared_input(state.paths, 'steer', target=target.request_id)
    state.observe()
    claimed = state.dispatch()
    assert (claimed, len(state.claims), len(state.submissions)) == (0, 0, 0)
    terminal = read_gateway_terminal_envelope_report(state.paths.terminal / f'{pending.request_id}.json').payload
    response = terminal['terminal_response']
    assert (response['id'], response['status'], response['error_code'], response['expected_turn_id']) == (
        pending.request_id, 'cancelled', 'EXPIRED_TARGET_TURN', target.request_id)
    assert 'execution_attempt_id' not in terminal
    assert not (state.paths.inbox / f'{pending.request_id}.json').exists()
    assert not (state.paths.processing / f'{pending.request_id}.json').exists()
    assert inputs.read_gateway_input_receipt(state.paths, pending.request_id).state == 'terminal_unknown'
    assert read_gateway_terminal_envelope_report(state.paths.terminal / f'{target.request_id}.json',
                                                include_sealed=True).payload == canonical
    assert state.dispatch() == 0
    assert state.claims == state.submissions == []


@pytest.mark.parametrize('status', ['done', 'failed'])
def test_sealed_completed_target_allows_one_next_turn(steer_dispatch, status):
    state = steer_dispatch
    target = _prepared_input(state.paths, 'target')
    canonical = _finish_input(state.paths, target, status)
    _seal(state.paths)
    pending = _prepared_input(state.paths, 'next', target=target.request_id)
    state.observe()
    assert state.dispatch() == 1
    assert state.dispatch() == 0
    assert state.claims == state.submissions == [pending.request_id]
    assert read_gateway_terminal_envelope_report(state.paths.terminal / f'{target.request_id}.json',
                                                include_sealed=True).payload == canonical


@pytest.mark.parametrize('scope', [{'user': 'bob'}, {'conversation': 'another-thread'}], ids=['owner', 'thread'])
def test_sealed_target_scope_mismatch_keeps_pending_unclaimed(steer_dispatch, scope):
    state = steer_dispatch
    target = _prepared_input(state.paths, 'target', **scope)
    canonical = _finish_input(state.paths, target, 'cancelled')
    _seal(state.paths)
    pending = _prepared_input(state.paths, 'steer', target=target.request_id)
    with pytest.raises(DataCorruptionError, match='queued_steer_scope_conflict') as error:
        expiry.queued_steer_expiry_response(state.paths, pending.prepared_request)
    assert error.value.__cause__ is None
    assert expiry.queued_steer_pre_acquire(state.paths, pending.prepared_request) == 'target_turn_unavailable'
    state.observe()
    assert state.dispatch() == 0
    assert state.claims == state.submissions == []
    assert (state.paths.inbox / f'{pending.request_id}.json').exists()
    assert not (state.paths.terminal / f'{pending.request_id}.json').exists()
    assert read_gateway_terminal_envelope_report(state.paths.terminal / f'{target.request_id}.json',
                                                include_sealed=True).payload == canonical


def test_unreadable_sealed_target_keeps_pending_unclaimed(steer_dispatch):
    state = steer_dispatch
    target = _prepared_input(state.paths, 'target')
    _finish_input(state.paths, target, 'cancelled')
    _seal(state.paths)
    pending = _prepared_input(state.paths, 'steer', target=target.request_id)
    segment, = (state.paths.root / 'archive').glob('receipts-seg-*.jsonl')
    segment.write_bytes(b'{broken archive')
    with pytest.raises(DataCorruptionError, match='target_terminal_unreadable') as error:
        expiry.queued_steer_expiry_response(state.paths, pending.prepared_request)
    assert error.value.__cause__ is None
    assert expiry.queued_steer_pre_acquire(state.paths, pending.prepared_request) == 'target_turn_unavailable'
    state.observe()
    assert state.dispatch() == 0
    assert state.claims == state.submissions == []
    assert (state.paths.inbox / f'{pending.request_id}.json').exists()
    assert inputs.read_gateway_input_receipt(state.paths, pending.request_id).state == 'queued'


def test_sealed_stop_expiry_recovers_before_new_turn(steer_dispatch, monkeypatch):
    state = steer_dispatch
    target = _prepared_input(state.paths, 'active')
    queued = _prepared_input(state.paths, 'steer', target=target.request_id)
    canonical = _finish_input(state.paths, target, 'cancelled')
    _seal(state.paths)
    project = expiry.project_expired_steer

    def interrupted_projection(*args, **kwargs):
        raise OSError('projection interrupted after canonical commit')

    monkeypatch.setattr(expiry, 'project_expired_steer', interrupted_projection)
    state.observe()
    assert state.dispatch() == 0
    path = state.paths.terminal / f'{queued.request_id}.json'
    committed = read_gateway_terminal_envelope_report(path).payload
    assert committed['terminal_response']['error_code'] == 'EXPIRED_TARGET_TURN'
    assert (state.paths.inbox / path.name).exists()
    assert inputs.read_gateway_input_receipt(state.paths, queued.request_id).state == 'queued'
    assert state.claims == state.submissions == []
    # 原输入及取消证明保持到投影确认；恢复不得再次读取/依赖旧目标。
    monkeypatch.setattr(expiry, 'project_expired_steer', project)
    with monkeypatch.context() as recovery:
        recovery.setattr(archive, '_receipt_index', lambda *_a, **_k: pytest.fail('recovery reopened target index'))
        assert state.dispatch() == 0
        assert state.dispatch() == 0
    assert read_gateway_terminal_envelope_report(path).payload == committed
    assert not (state.paths.inbox / path.name).exists()
    assert inputs.read_gateway_input_receipt(state.paths, queued.request_id).state == 'terminal_unknown'
    next_turn = _prepared_input(state.paths, 'new-turn')
    assert state.dispatch() == 1
    assert state.dispatch() == 0
    assert state.claims == state.submissions == [next_turn.request_id]
    assert read_gateway_terminal_envelope_report(path).payload == committed
    assert read_gateway_terminal_envelope_report(state.paths.terminal / f'{target.request_id}.json',
                                                include_sealed=True).payload == canonical
