"""派生回执索引损坏与已绑定 queued steer 的闭环回归。"""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
from agent_py_agent.agent.gateway_parts import owner_retention as archive
from agent_py_agent.agent.gateway_parts import queued_steer_expiry as expiry
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context
from agent_py_agent.tests.test_gateway_receipt_archive import _settled
from agent_py_agent.tests.test_v13_w4_conversation_steer_stop import (
    _queued_prepared_steer,
    _terminal_target,
)


def _corrupt_index(tmp_path, damage='header'):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    canonical, _, _ = _settled(paths)
    assert archive.seal_gateway_receipts_slice(paths, before=100, max_ms=1000)['sealed_requests'] == 1
    index = paths.root / 'archive' / 'receipts-index.sqlite3'
    raw = index.read_bytes()
    damaged = b'not a SQLite index' if damage == 'header' else raw[:100]
    index.write_bytes(damaged)
    return paths, index, damaged, canonical


@pytest.mark.parametrize('damage,code', [('header', sqlite3.SQLITE_NOTADB), ('pages', sqlite3.SQLITE_CORRUPT)])
def test_confirmed_corruption_rebuilt_only_by_authorized_maintenance(tmp_path, damage, code):
    paths, index, damaged, canonical = _corrupt_index(tmp_path, damage)
    manifest = paths.root / 'archive' / 'receipts-manifest.json'
    before = {p.name: p.read_bytes() for p in manifest.parent.glob('receipts-*.json*')}
    with pytest.raises(sqlite3.DatabaseError) as proof:
        db = sqlite3.connect(index)
        try:
            db.execute('PRAGMA table_info(receipts)').fetchall()
        finally:
            db.close()
    assert proof.value.sqlite_errorcode & 0xFF == code
    with pytest.raises(DataCorruptionError, match='GATEWAY_ARCHIVE_INDEX_CORRUPT'):
        archive.lookup_sealed_receipt(paths, 'requests/terminal/request-a.json', no_repair=True)
    assert index.read_bytes() == damaged
    assert not (manifest.parent / 'receipts-index-quarantine').exists()
    result = archive.seal_gateway_receipts_slice(paths, before=100, max_ms=1000)
    assert result['errors'] == []
    assert archive.lookup_sealed_receipt(paths, 'requests/terminal/request-a.json', no_repair=True) is not None
    assert read_json_file(paths.terminal / 'request-a.json') == canonical
    quarantined = tuple(manifest.parent.glob('receipts-index-quarantine/*/*/receipts-index.sqlite3'))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == damaged
    assert before == {p.name: p.read_bytes() for p in manifest.parent.glob('receipts-*.json*')}
    assert not (paths.terminal / 'request-a.json').exists()


@pytest.mark.parametrize('code', [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_IOERR, None])
def test_other_database_errors_preserved_without_quarantine(tmp_path, monkeypatch, code):
    paths, index, damaged, _ = _corrupt_index(tmp_path)
    error = sqlite3.DatabaseError('not a database: prose is not repair authority')
    if code is not None:
        error.sqlite_errorcode = code
    def denied(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(archive.sqlite3, 'connect', denied)
    with pytest.raises(sqlite3.DatabaseError) as observed:
        archive._receipt_index(paths, writable=True)
    assert observed.value is error
    assert index.read_bytes() == damaged
    assert not (index.parent / 'receipts-index-quarantine').exists()


def test_failed_rebuild_preserves_corrupt_index_and_canonical_evidence(tmp_path):
    paths, index, damaged, _ = _corrupt_index(tmp_path)
    manifest = index.parent / 'receipts-manifest.json'
    manifest.write_bytes(b'{invalid')
    with pytest.raises((DataCorruptionError, ValueError)):
        archive.seal_gateway_receipts_slice(paths, before=100, max_ms=1000)
    assert index.read_bytes() == damaged
    assert manifest.read_bytes() == b'{invalid'
    assert not (index.parent / 'receipts-index-quarantine').exists()


def _committed_bound_steer(control, *, receipt_state='queued', bound_target='target'):
    receipt = _queued_prepared_steer(control)
    receipt = replace(receipt, target_turn_id=bound_target, state=receipt_state)
    with inputs.gateway_input_transition(control.paths, receipt.request_id):
        write_json_file_atomic(inputs.gateway_input_receipt_path(control.paths, receipt.request_id), receipt.to_dict())
    _terminal_target(control.paths, source=receipt.prepared_request)
    path = control.paths.inbox / (receipt.request_id + '.json')
    with inputs.gateway_input_transition(control.paths, receipt.request_id):
        assert expiry.expire_queued_steer_locked(control.paths, path, receipt.prepared_request, retain_inbox=True)
    return receipt, path


@pytest.mark.parametrize('multiprocess', [False, True])
def test_expired_bound_queued_receipt_retires_without_replay(context, monkeypatch, multiprocess):
    receipt, path = _committed_bound_steer(context)
    canonical = context.paths.terminal / path.name
    before = canonical.read_bytes()
    gate = None
    if multiprocess:
        from agent_py_agent.tests.test_gateway_durable_dispatch_scope import _gate
        gate, _ = _gate(context, monkeypatch)
    assert expiry.resume_expired_steer(context.paths, path, admission_gate=gate) == 'expired'
    assert not path.exists()
    assert inputs.read_gateway_input_receipt(context.paths, receipt.request_id).state == 'terminal_unknown'
    assert canonical.read_bytes() == before
    assert not (context.paths.processing / path.name).exists()


@pytest.mark.parametrize('unknown', ['pending', 'wrong_binding', 'target_missing', 'target_corrupt', 'target_processing', 'target_owner'])
def test_expiry_preserves_unproven_bound_input(context, unknown):
    receipt, path = _committed_bound_steer(context,
        receipt_state='pending' if unknown == 'pending' else 'queued',
        bound_target='other-turn' if unknown == 'wrong_binding' else 'target')
    target = context.paths.terminal / 'target.json'
    if unknown == 'target_missing':
        target.unlink()
    elif unknown == 'target_corrupt':
        target.write_bytes(b'{invalid')
    elif unknown == 'target_processing':
        write_json_file_atomic(context.paths.processing / 'target.json', {'id': 'target'})
    elif unknown == 'target_owner':
        payload = read_json_file(target)
        payload['owner_id'] = 'other-owner'
        write_json_file_atomic(target, payload)
    before = inputs.gateway_input_receipt_path(context.paths, receipt.request_id).read_bytes()
    assert expiry.resume_expired_steer(context.paths, path) == 'blocked'
    assert path.exists()
    assert inputs.gateway_input_receipt_path(context.paths, receipt.request_id).read_bytes() == before


def test_corrupt_index_with_local_sqlite_reader_waits_for_quiescence(tmp_path):
    paths, index, damaged, _ = _corrupt_index(tmp_path)
    stamp = index.stat()
    holder = sqlite3.connect(index)
    try:
        with pytest.raises(BlockingIOError, match='local_reader_attached'):
            archive.seal_gateway_receipts_slice(paths, before=100, max_ms=1000)
        current = index.stat()
        assert (current.st_ino, current.st_size, current.st_mtime_ns) == (stamp.st_ino, stamp.st_size, stamp.st_mtime_ns)
        assert not (index.parent / 'receipts-index-quarantine').exists()
    finally:
        holder.close()
    assert index.read_bytes() == damaged
    assert archive.seal_gateway_receipts_slice(paths, before=100, max_ms=1000)['errors'] == []
