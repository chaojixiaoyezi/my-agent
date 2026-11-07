"""终态原件删除后仍按封存索引拒绝重放，损坏索引保持终态未知。"""
from __future__ import annotations

import sqlite3
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import (
    control_service,
    execution_target,
    input_delivery_service,
    multiprocess_admission,
    multiprocess_processing_recovery,
    multiprocess_takeover_runtime,
    multiprocess_unroutable_closeout,
    queued_steer_expiry,
    recovery,
    request_worker,
)
from agent_py_agent.agent.gateway_parts import (
    owner_retention as archive,
)
from agent_py_agent.agent.gateway_parts.io import read_json_file_report, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import GatewayClaim
from agent_py_agent.agent.gateway_parts.request_binding import GatewayActiveTurnTransition
from agent_py_agent.agent.runtime_errors import DataCorruptionError


@pytest.fixture
def sealed(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    key = 'sealed-request'
    original = {
        'id': key, 'kind': 'ask', 'user_id': 'alice', 'prompt': 'hello',
        'status': 'processing', 'turn_phase': 'open',
        'execution_attempt_id': 'attempt-a', 'lease_epoch': 1, 'lease_owner': 'worker-a',
        'lease_heartbeat_at': 1,
        'metadata': {'client_input_digest': 'digest-a'},
        'conversation': {'channel': 'test', 'channel_conversation_id': 'room'},
    }
    response = {'id': key, 'ok': True, 'status': 'done', 'ended_at': 1, 'output': '已完成'}
    canonical = recovery._terminal_gateway_request_payload(key, original, response)
    for folder, body in ((paths.terminal, canonical), (paths.done, canonical), (paths.responses, response)):
        write_json_file_atomic(folder / f'{key}.json', body)
    recovery.mark_gateway_terminal_projection_complete(paths, key, response)
    result = archive.seal_gateway_receipts_slice(paths, before=100, max_ms=1000)
    assert result['sealed_requests'] == 1 and not result['errors'], result
    assert not (paths.terminal / f'{key}.json').exists()
    return SimpleNamespace(paths=paths, key=key, original=original, canonical=canonical, response=response,
                           index=paths.root / 'archive' / 'receipts-index.sqlite3')


def _processing(case, *, status='processing'):
    path = case.paths.processing / f'{case.key}.json'
    write_json_file_atomic(path, dict(case.original, status=status))
    return path


def _corrupt_index(case):
    case.index.write_bytes(b'not a SQLite database')


def test_loose_terminal_short_circuits_without_opening_index(sealed, monkeypatch):
    calls = []
    monkeypatch.setattr(archive, '_receipt_index', lambda *_: calls.append(True))
    write_json_file_atomic(sealed.paths.terminal / f'{sealed.key}.json', sealed.canonical)
    assert archive.gateway_terminal_exists(sealed.paths, f'{sealed.key}.json')
    assert calls == []


def test_sealed_presence_and_absence_use_index_without_reading_body(sealed, monkeypatch):
    def no_segment_read(*args, **kwargs):
        pytest.fail('存在判断不能读取归档段正文')

    monkeypatch.setattr(archive, 'lookup_sealed_receipt', no_segment_read)
    monkeypatch.setattr(archive, 'read_bytes_beneath', no_segment_read)
    monkeypatch.setattr(archive, 'open_directory_beneath', no_segment_read)
    assert archive.gateway_terminal_exists(sealed.paths, f'{sealed.key}.json')
    assert not archive.gateway_terminal_exists(sealed.paths, 'never-existed.json')
    assert archive.sealed_receipt_exists(sealed.paths, f'requests/terminal/{sealed.key}.json')


def test_no_manifest_only_checks_loose_terminal(sealed, monkeypatch):
    (sealed.index.parent / 'receipts-manifest.json').unlink()
    monkeypatch.setattr(archive, '_receipt_index', lambda *_: pytest.fail('无 manifest 不打开索引'))
    assert not archive.gateway_terminal_exists(sealed.paths, f'{sealed.key}.json')
    write_json_file_atomic(sealed.paths.terminal / f'{sealed.key}.json', sealed.canonical)
    assert archive.gateway_terminal_exists(sealed.paths, f'{sealed.key}.json')


@pytest.mark.parametrize('relative', ['', '.', '..', '../escape.json', '/absolute.json'])
def test_invalid_receipt_path_is_rejected_without_index(tmp_path, monkeypatch, relative):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    monkeypatch.setattr(archive, '_receipt_index', lambda *_: pytest.fail('非法路径不能查索引'))
    with pytest.raises(ValueError):
        archive.sealed_receipt_exists(paths, relative)


def test_corrupt_sqlite_is_not_absence(sealed):
    _corrupt_index(sealed)
    with pytest.raises(DataCorruptionError):
        archive.gateway_terminal_exists(sealed.paths, f'{sealed.key}.json')


def test_existing_corruption_error_is_propagated_unchanged(sealed, monkeypatch):
    error = DataCorruptionError('gateway_archive_index_invalid')

    def fail(*args):
        raise error

    monkeypatch.setattr(archive, '_receipt_index', fail)
    with pytest.raises(DataCorruptionError) as caught:
        archive.gateway_terminal_exists(sealed.paths, f'{sealed.key}.json')
    assert caught.value is error


@pytest.mark.parametrize('broken', [False, True])
def test_index_query_is_exact_and_connection_is_closed(sealed, monkeypatch, broken):
    calls, closed = [], []

    def execute(sql, values):
        calls.append((sql, values))
        if broken:
            raise sqlite3.DatabaseError('injected query failure')
        return SimpleNamespace(fetchone=lambda: (1,))

    monkeypatch.setattr(archive, '_receipt_index', lambda *_: SimpleNamespace(
        execute=execute, close=lambda: closed.append(True)))
    if broken:
        with pytest.raises(DataCorruptionError):
            archive.gateway_terminal_exists(sealed.paths, f'{sealed.key}.json')
    else:
        assert archive.gateway_terminal_exists(sealed.paths, f'{sealed.key}.json')
    assert calls == [('SELECT 1 FROM receipts WHERE path=?', (f'requests/terminal/{sealed.key}.json',))]
    assert closed == [True]


def test_unified_json_reader_still_reads_sealed_body(sealed):
    report = read_json_file_report(sealed.paths.terminal / f'{sealed.key}.json')
    assert report.load_error is None and report.payload == sealed.canonical
    response = read_json_file_report(sealed.paths.responses / f'{sealed.key}.json')
    assert response.load_error is None and response.payload == sealed.response


@pytest.mark.parametrize('corrupt', [False, True])
def test_admission_does_not_reuse_terminal_processing(sealed, monkeypatch, corrupt):
    path = _processing(sealed, status='pending')
    if corrupt:
        _corrupt_index(sealed)
    monkeypatch.setattr(multiprocess_admission, '_read_reservation',
                        lambda *_: pytest.fail('终态或未知终态不能继续准入'))
    gate = SimpleNamespace(paths=sealed.paths)
    result = multiprocess_admission._processing_recovery_decision_locked(gate, path, None, None)
    assert result['status'] == 'blocked'
    assert result['reason'] == ('processing_recovery_terminal_conflict' if corrupt
                                else 'processing_recovery_path_conflict')


@pytest.mark.parametrize('corrupt', [False, True])
def test_processing_recovery_never_requeues_sealed_request(sealed, corrupt):
    path = _processing(sealed, status='pending')
    before = path.read_bytes()
    if corrupt:
        _corrupt_index(sealed)
    report = recovery.recover_gateway_processing_requests_report(sealed.paths, startup=True)
    assert report.summary['requeued'] == 0
    assert not (sealed.paths.inbox / path.name).exists()
    if corrupt:
        assert report.load_errors and path.read_bytes() == before
    else:
        assert report.summary['archived'] == 1 and not report.load_errors
        assert not path.exists()
        assert read_json_file_report(sealed.paths.terminal / path.name).payload == sealed.canonical


@pytest.mark.parametrize('corrupt', [False, True])
def test_worker_does_not_reopen_capability_closeout(sealed, corrupt):
    path = _processing(sealed)
    before = path.read_bytes()
    manager = SimpleNamespace(creation_guard=nullcontext)
    agent = SimpleNamespace(subagents=manager)
    if corrupt:
        _corrupt_index(sealed)
        with pytest.raises(DataCorruptionError):
            request_worker._close_claimed_parent_capabilities(
                sealed.paths, path, sealed.key, sealed.response, agent, 'attempt-a', 1)
    else:
        request_worker._close_claimed_parent_capabilities(
            sealed.paths, path, sealed.key, sealed.response, agent, 'attempt-a', 1)
    assert path.read_bytes() == before
    assert 'capability_closeout' not in read_json_file_report(path).payload


@pytest.mark.parametrize('corrupt', [False, True])
def test_control_does_not_accept_input_for_sealed_turn(sealed, corrupt):
    path = _processing(sealed)
    record = control_service._GatewayRequestRecord(path, sealed.original)
    scope = control_service.GatewayControlScope('alice', 'test', 'room')
    if corrupt:
        _corrupt_index(sealed)
    assert control_service._load_open_exact_turn(sealed.paths, record, scope, sealed.key) is None


@pytest.mark.parametrize('corrupt', [False, True])
def test_active_turn_cannot_cross_provider_boundary(sealed, corrupt):
    path = _processing(sealed)
    if corrupt:
        _corrupt_index(sealed)
    transition = GatewayActiveTurnTransition(path, sealed.key, 'attempt-a')
    with pytest.raises(InterruptedError):
        transition('submit', lambda: pytest.fail('终态或未知终态不能重新提交模型'))


@pytest.mark.parametrize('corrupt', [False, True])
def test_execution_target_does_not_project_sealed_foreground(sealed, corrupt):
    path = _processing(sealed)
    task_id = f'gateway:{sealed.key}'
    write_json_file_atomic(path, dict(sealed.original, conversation_claim={
        'schema_version': 'gateway_conversation_claim.v1', 'request_id': sealed.key,
        'thread_id': 'thread-a', 'task_id': task_id,
    }))
    if corrupt:
        _corrupt_index(sealed)
    assert execution_target._foreground_target(
        sealed.paths, {'task_id': task_id, 'claim_id': 'claim-a'}, 'thread-a') is None


@pytest.mark.parametrize('corrupt', [False, True])
def test_interrupted_queue_move_does_not_restore_terminal_request(sealed, corrupt):
    path = _processing(sealed, status='pending')
    payload = read_json_file_report(path).payload
    context = recovery._RecoveryContext(time.time() + 1000, 3, 10, True, None, None)
    if corrupt:
        _corrupt_index(sealed)
    with pytest.raises(DataCorruptionError):
        recovery._finish_interrupted_queue_move(sealed.paths, path, payload, context)
    assert path.exists() and not (sealed.paths.inbox / path.name).exists()


@pytest.mark.parametrize('corrupt', [False, True])
def test_current_requeue_does_not_restore_terminal_request(sealed, corrupt):
    path = _processing(sealed)
    claim = GatewayClaim(path, sealed.key, 'attempt-a', 1, 'worker-a')
    if corrupt:
        _corrupt_index(sealed)
    with pytest.raises(DataCorruptionError):
        recovery._requeue_current_processing_locked(sealed.paths, claim, time.time() + 100)
    assert path.exists() and not (sealed.paths.inbox / path.name).exists()


@pytest.mark.parametrize('corrupt', [False, True])
def test_terminal_commit_preserves_existing_sealed_outcome(sealed, corrupt):
    path = _processing(sealed)
    conflict = recovery._terminal_gateway_request_payload(
        sealed.key, sealed.original, dict(sealed.response, output='另一个结果'))
    if corrupt:
        _corrupt_index(sealed)
    with pytest.raises((DataCorruptionError, RuntimeError)):
        recovery._commit_gateway_terminal_request(sealed.paths, path, sealed.paths.done, sealed.key, conflict)
    assert path.exists() and not (sealed.paths.terminal / path.name).exists()


@pytest.mark.parametrize('corrupt', [False, True])
def test_input_lifecycle_keeps_sealed_terminal_authority(sealed, corrupt):
    _processing(sealed)
    if corrupt:
        _corrupt_index(sealed)
    assert input_delivery_service._gateway_turn_lifecycle_locked(sealed.paths, sealed.key) == (
        'unknown' if corrupt else 'terminal')


def test_input_terminal_without_hot_propagates_corruption(sealed):
    assert input_delivery_service._terminal_request_without_hot(sealed.paths, sealed.key)
    _corrupt_index(sealed)
    with pytest.raises(DataCorruptionError):
        input_delivery_service._terminal_request_without_hot(sealed.paths, sealed.key)


def test_expired_steer_blocks_when_terminal_index_is_unknown(sealed):
    _corrupt_index(sealed)
    assert queued_steer_expiry.resume_expired_steer(
        sealed.paths, sealed.paths.inbox / f'{sealed.key}.json') == 'blocked'


@pytest.mark.parametrize('corrupt', [False, True])
def test_admission_release_only_uses_known_terminal(sealed, monkeypatch, corrupt):
    released = []
    monkeypatch.setattr(multiprocess_admission, 'release_reservation_if_terminal',
                        lambda gate, key: released.append(key) or {'released': True})
    if corrupt:
        _corrupt_index(sealed)
    assert multiprocess_admission._release_settled_if_terminal(
        SimpleNamespace(paths=sealed.paths), sealed.key) is not corrupt
    assert released == ([] if corrupt else [sealed.key])


@pytest.mark.parametrize('corrupt', [False, True])
def test_takeover_does_not_issue_recovery_for_sealed_pending(sealed, monkeypatch, corrupt):
    path = _processing(sealed, status='pending')
    monkeypatch.setattr(multiprocess_processing_recovery, 'original_processing_target',
                        lambda *_: pytest.fail('终态或未知终态不能取得恢复来源'))
    if corrupt:
        _corrupt_index(sealed)
    assert multiprocess_takeover_runtime._processing_request_target(
        SimpleNamespace(targets=[]), SimpleNamespace(paths=sealed.paths), path, ('owner', 'session')) is None


def test_unroutable_resume_blocks_on_unknown_terminal(sealed):
    _corrupt_index(sealed)
    gate = multiprocess_admission.PersistentAdmission(
        sealed.paths, None, SimpleNamespace(count=1, c_run_id='current'))
    assert multiprocess_unroutable_closeout.resume_unroutable_closeout(
        sealed.paths, sealed.paths.inbox / f'{sealed.key}.json', admission_gate=gate) == 'blocked'


@pytest.mark.parametrize('corrupt', [False, True])
def test_worker_does_not_refresh_wait_projection_for_terminal(sealed, monkeypatch, corrupt):
    path = sealed.paths.inbox / f'{sealed.key}.json'
    pending = dict(sealed.original, status='pending')
    pending.pop('execution_attempt_id')
    write_json_file_atomic(path, pending)
    before = path.read_bytes()
    gate = SimpleNamespace(paths=sealed.paths)
    monkeypatch.setattr(multiprocess_admission, '_runtime_commit_scope', lambda *_: nullcontext())
    if corrupt:
        _corrupt_index(sealed)
    assert not request_worker._write_admission_wait_field(
        path, {'admission_wait_at': 1}, paths=sealed.paths, admission_gate=gate)
    assert path.read_bytes() == before


def test_recovery_failure_observes_sealed_terminal_without_losing_original_error(sealed):
    path = _processing(sealed)
    action, report = recovery._recovery_failure_observation(
        sealed.paths, path, sealed.original, OSError('injected projection failure'))
    assert action == 'archived' and report['terminal_committed']
    assert report['error_type'] == 'OSError'


@pytest.mark.parametrize('corrupt', [False, True])
def test_control_receipt_lifecycle_prefers_sealed_terminal(sealed, corrupt):
    _processing(sealed)
    if corrupt:
        _corrupt_index(sealed)
    scope = control_service.GatewayControlScope('alice', 'test', 'room')
    assert control_service._exact_gateway_turn_lifecycle_locked(sealed.paths, scope, sealed.key) == (
        'unknown' if corrupt else 'terminal')


@pytest.mark.parametrize('corrupt', [False, True])
def test_input_materialization_does_not_recreate_sealed_request(sealed, corrupt):
    receipt = input_delivery_service.GatewayInputReceipt(
        request_id=sealed.key, client_input_digest='digest-a', client_message_id=sealed.key,
        state='queued', prepared_request=sealed.original,
        prepared_payload_digest=input_delivery_service._prepared_payload_digest(sealed.original),
        guidance_dedupe_key=sealed.key,
    )
    if corrupt:
        _corrupt_index(sealed)
        with pytest.raises(DataCorruptionError):
            input_delivery_service._materialize_prepared_request_locked(sealed.paths, receipt)
    else:
        assert not input_delivery_service._materialize_prepared_request_locked(sealed.paths, receipt)
    assert not (sealed.paths.inbox / f'{sealed.key}.json').exists()
