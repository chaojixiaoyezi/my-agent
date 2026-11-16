"""精确待办/终态收敛和只读投影副作用回归。"""
import sqlite3

import pytest

from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import (
    claim_request,
    ensure_gateway_folders,
    observe_pending_request,
)
from agent_py_agent.agent.gateway_parts.recovery import _terminal_gateway_request_payload
from agent_py_agent.agent.gateway_parts.recovery_pending_index import (
    ProjectionReadUnavailableError,
    private_projection_connection,
)


def test_duplicate_retirement_preserves_canonical(tmp_path):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    ensure_gateway_folders(paths)
    pending = paths.inbox / 'request.json'
    payload = {'id': 'request', 'kind': 'ask', 'goal': '原请求', 'user_id': 'owner'}
    write_json_file_atomic(pending, payload)
    terminal = _terminal_gateway_request_payload('request', payload, {'id': 'request', 'ok': True, 'answer': '完成'})
    canonical = paths.terminal / pending.name
    write_json_file_atomic(canonical, terminal)
    original = canonical.read_bytes()
    assert observe_pending_request(paths, 'request')['state'] == 'duplicate_terminal'
    assert claim_request(paths, pending) is None
    assert not pending.exists() and canonical.read_bytes() == original
    assert read_json_file(paths.done / pending.name) == terminal
    assert claim_request(paths, pending) is None


@pytest.mark.parametrize('change', ['goal', 'fingerprint', 'processing', 'inline'])
def test_conflicting_or_active_pending_protected(tmp_path, change):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    ensure_gateway_folders(paths)
    payload = {'id': 'request', 'kind': 'ask', 'goal': '原请求', 'user_id': 'owner'}
    terminal = _terminal_gateway_request_payload('request', payload, {'id': 'request', 'ok': True})
    write_json_file_atomic(paths.terminal / 'request.json', terminal)
    if change == 'goal':
        payload['goal'] = '其它请求'
    elif change == 'fingerprint':
        payload['request_fingerprint'] = terminal['request_fingerprint']
        payload['goal'] = '其它请求'
    elif change == 'processing':
        write_json_file_atomic(paths.processing / 'request.json', payload)
    else:
        payload.update(source='plugin', metadata={'plugin_provenance': {'kind': 'tool'}})
    pending = paths.inbox / 'request.json'
    write_json_file_atomic(pending, payload)
    before = pending.read_bytes()
    assert observe_pending_request(paths, 'request')['state'] == 'protected'
    assert claim_request(paths, pending) is None
    assert pending.read_bytes() == before


def test_projection_missing_read_never_creates(tmp_path):
    path = tmp_path / 'missing' / 'index.sqlite3'
    before = tuple(tmp_path.iterdir())
    with pytest.raises(ProjectionReadUnavailableError):
        with private_projection_connection(path, read_only=True):
            pass
    assert tuple(tmp_path.iterdir()) == before


def test_projection_read_wal_without_schema_or_file_mutation(tmp_path):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    path = directory / 'index.sqlite3'
    writer = sqlite3.connect(path)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE facts (value TEXT)')
        writer.execute("INSERT INTO facts VALUES ('committed')")
        writer.commit()
        before = {p.name: (p.stat().st_mode, p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
                  for p in directory.iterdir()}
        with private_projection_connection(path, read_only=True, blocking=False) as db:
            assert db.execute('SELECT value FROM facts').fetchall() == [('committed',)]
            with pytest.raises(sqlite3.OperationalError):
                db.execute("INSERT INTO facts VALUES ('forbidden')")
        after = {p.name: (p.stat().st_mode, p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns)
                 for p in directory.iterdir()}
        assert before.keys() == after.keys()
        for name in before:
            if name.endswith('-shm'):
                # SQLite 原生读锁可更新 read-mark；不改变文件身份、权限或尺寸。
                assert before[name][:2] == after[name][:2]
                assert before[name][2] == after[name][2]
            else:
                assert before[name] == after[name]
    finally:
        writer.close()


def test_cold_wal_database_remains_functionally_readable(tmp_path):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    path = directory / 'cold.sqlite3'
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE facts(value)')
    db.execute('INSERT INTO facts VALUES (17)')
    db.commit()
    db.close()
    original = path.read_bytes()
    with private_projection_connection(path, read_only=True) as db:
        assert db.execute('SELECT value FROM facts').fetchone() == (17,)
        assert db.execute('PRAGMA user_version').fetchone() == (0,)
    assert path.read_bytes() == original
    assert not path.with_name(path.name + '.lock').exists()
    assert all(p.name in {'cold.sqlite3', 'cold.sqlite3-wal', 'cold.sqlite3-shm'} for p in directory.iterdir())


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_readonly_rejects_malicious_sidefile_without_touching_target(tmp_path, suffix):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    path = directory / 'index.sqlite3'
    sqlite3.connect(path).close()
    target = tmp_path / 'protected'
    target.write_bytes(b'untouched')
    path.with_name(path.name + suffix).symlink_to(target)
    with pytest.raises(ProjectionReadUnavailableError):
        with private_projection_connection(path, read_only=True):
            pytest.fail('malicious sidefile was opened')
    assert target.read_bytes() == b'untouched'


def test_readonly_preserves_caller_error_identity(tmp_path):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    path = directory / 'index.sqlite3'
    sqlite3.connect(path).close()
    error = ValueError('cursor_generation_changed')
    with pytest.raises(ValueError) as observed:
        with private_projection_connection(path, read_only=True):
            raise error
    assert observed.value is error


def test_archived_terminal_duplicate_retired_without_reexecution(tmp_path):
    from agent_py_agent.agent.gateway_parts.owner_retention import seal_gateway_receipts_slice
    from agent_py_agent.tests.test_gateway_receipt_archive import _settled
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    terminal, _response, _chunk = _settled(paths)
    report = seal_gateway_receipts_slice(paths, before=100, max_entries=4, max_ms=1000)
    assert report['sealed_requests'] == 1
    pending = paths.inbox / 'request-a.json'
    write_json_file_atomic(pending, terminal)
    manifest = paths.root / 'archive' / 'receipts-manifest.json'
    before = manifest.read_bytes()
    assert observe_pending_request(paths, 'request-a')['state'] == 'duplicate_terminal'
    assert claim_request(paths, pending) is None
    assert not pending.exists() and manifest.read_bytes() == before
    assert not (paths.terminal / pending.name).exists()


def test_archived_observation_never_rebuilds_missing_index(tmp_path):
    from agent_py_agent.agent.gateway_parts.owner_retention import seal_gateway_receipts_slice
    from agent_py_agent.tests.test_gateway_receipt_archive import _settled
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    terminal, _response, _chunk = _settled(paths)
    assert seal_gateway_receipts_slice(paths, before=100, max_entries=4, max_ms=1000)['sealed_requests'] == 1
    write_json_file_atomic(paths.inbox / 'request-a.json', terminal)
    index = paths.root / 'archive' / 'receipts-index.sqlite3'
    index.unlink()
    before = {p.relative_to(paths.root) for p in paths.root.rglob('*')}
    result = observe_pending_request(paths, 'request-a')
    assert result['coverage'] == 'unavailable' and result['state'] == 'protected'
    assert not index.exists()
    assert before == {p.relative_to(paths.root) for p in paths.root.rglob('*')}
