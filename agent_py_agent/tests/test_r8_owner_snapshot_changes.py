"""R13：温缓存仅访问已证明的变更；冷/丢事件/远端换代均保持恢复事实。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.owner_change_journal import OwnerChangeBatch
from agent_py_agent.agent.owner_object_store import OwnerManifestDelta
from agent_py_agent.agent.owner_snapshot_cache import OwnerSnapshotIndex
from agent_py_agent.tests.test_s1w2_owner_snapshot_delta import (
    _checkout,
    _delta_store,
    _seed,
    _write_old,
)


class Journal:
    def __init__(self):
        self.sequence = 0
        self.events = []
        self.fresh = False
        self.closed = False

    def record(self, path, *, exists=True, type='f', new=False):
        self.sequence += 1
        self.events.append((self.sequence, {'name': 'owners/u1/' + path, 'exists': exists, 'type': type, 'new': new}))

    def query(self, root, since='', **kwargs):
        old = int(since.rsplit(':', 1)[-1]) if since else -1
        self.sequence += 1
        clock = f'c:test:{self.sequence}'
        if not since or self.fresh:
            self.fresh = False
            return OwnerChangeBatch(clock, (), False, 'observer_fresh_instance')
        return OwnerChangeBatch(clock, tuple(event for sequence, event in self.events if sequence > old), True, '')

    def close(self):
        self.closed = True

    def release(self, root):
        pass


def _case(tmp_path, count=1):
    store, warm, home = _delta_store(tmp_path)
    journal = store._changes = Journal()
    for number in range(count):
        _seed(store, f'files/{number}', str(number).encode())
    with _checkout(store, warm, home):
        pass
    store._client.downloads.clear()
    store._client.uploads.clear()
    store._client.heads.clear()
    return store, warm, home, journal


@pytest.mark.parametrize('count', [8, 2048])
def test_warm_round_does_not_read_history_manifest_scan_tree_or_rewrite_json(tmp_path, monkeypatch, count):
    store, warm, home, journal = _case(tmp_path, count)

    def forbidden(*args, **kwargs):
        raise AssertionError('warm round touched complete history')

    store._read_manifest = forbidden
    store._warm_paths = forbidden
    original_manifest = OwnerSnapshotIndex.manifest

    def bounded_manifest(self, paths=None):
        assert paths is not None
        return original_manifest(self, paths)

    monkeypatch.setattr(OwnerSnapshotIndex, 'manifest', bounded_manifest)
    monkeypatch.setattr(OwnerSnapshotIndex, 'paths', forbidden)
    with _checkout(store, warm, home):
        _write_old(home / 'files/0', b'changed')
        journal.record('files/0')
    assert [row.relative_path for row in store._head.commits[-1][1]] == ['files/0']
    assert len(store._client.uploads) == 1 and not store._client.downloads
    assert not (warm / 'stat-index.json').exists()
    with OwnerSnapshotIndex.open(warm / 'snapshot-index.sqlite3') as index:
        assert index.metadata()['mode'] == 'incremental'


def test_remote_generation_delta_avoids_full_read_and_corrects_only_changed_files(tmp_path, monkeypatch):
    store, warm, home, journal = _case(tmp_path, 8)
    _seed(store, 'files/0', b'remote-new')
    del store._head.rows['files/1']
    store._head.version += 1
    requested = []

    def delta(*args, **kwargs):
        requested.append(kwargs)
        return OwnerManifestDelta(True, (('files/0', store._head.rows['files/0']), ('files/1', None)))

    store._read_manifest_delta = delta
    store._read_manifest = lambda *_: pytest.fail('remote delta read complete manifest')
    store._warm_paths = lambda *_: pytest.fail('remote delta scanned complete home')
    with _checkout(store, warm, home):
        assert (home / 'files/0').read_bytes() == b'remote-new'
        assert not (home / 'files/1').exists()
    assert len(requested) == 1
    assert len(store._client.downloads) == 1 and not store._client.uploads
    assert store._head.commits[-1] == ([], [])


def test_observer_fresh_instance_is_explicit_full_reconcile_then_warm_resumes(tmp_path):
    store, warm, home, journal = _case(tmp_path, 4)
    journal.fresh = True
    with _checkout(store, warm, home):
        pass
    with OwnerSnapshotIndex.open(warm / 'snapshot-index.sqlite3') as index:
        assert index.metadata()['mode'] == 'full_reconcile'
        assert index.metadata()['reason'] == 'observer_fresh_instance'
    store._read_manifest = lambda *_: pytest.fail('recovered observer did not resume incremental')
    with _checkout(store, warm, home):
        pass


def test_directory_rename_chmod_and_delete_keep_precise_final_manifest(tmp_path):
    store, warm, home, journal = _case(tmp_path, 4)
    with _checkout(store, warm, home):
        (home / 'files').rename(home / 'moved')
        (home / 'moved/0').chmod(0o400)
        (home / 'moved/1').unlink()
        journal.record('files', exists=False, type='d')
        journal.record('moved', type='d', new=True)
    assert set(store._head.rows) == {'moved/0', 'moved/2', 'moved/3'}
    assert store._head.rows['moved/0'].file_mode == 0o400
    assert store._head.commits[-1][0] == [f'files/{i}' for i in range(4)]


def test_generation_change_during_body_keeps_dirty_and_never_overwrites_remote(tmp_path):
    store, warm, home, journal = _case(tmp_path)
    before = len(store._head.commits)
    with pytest.raises(RuntimeError, match='generation changed'):
        with _checkout(store, warm, home):
            _write_old(home / 'files/0', b'local')
            journal.record('files/0')
            store._head.version += 1
    assert len(store._head.commits) == before
    assert json.loads((warm / 'state.json').read_text())['state'] == 'DIRTY'


def test_expired_remote_change_cursor_is_visible_full_recovery(tmp_path):
    store, warm, home, journal = _case(tmp_path)
    store._head.version += 100000
    store._read_manifest_delta = lambda *args, **kwargs: OwnerManifestDelta(False, (), 'remote_change_cursor_expired')
    with _checkout(store, warm, home):
        pass
    with OwnerSnapshotIndex.open(warm / 'snapshot-index.sqlite3') as index:
        assert index.metadata()['reason'] == 'remote_change_cursor_expired'
        assert index.metadata()['mode'] == 'full_reconcile'


def test_unproven_linux_backend_never_grants_incremental_and_is_released(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_change_journal as changes

    monkeypatch.setattr(changes.sys, 'platform', 'linux')
    observer = changes.OwnerChangeJournal()
    observer._start = lambda: None
    commands = []

    def request(command):
        commands.append(command)
        assert command[0] in {'watch', 'watch-del'}
        return {'watcher': 'unproven-backend'}

    observer._request = request
    try:
        first = observer.query(tmp_path)
        second = observer.query(tmp_path, 'c:old:1')
        assert not first.complete and not second.complete
        assert first.reason == second.reason == 'observer_ordering_unproven'
        assert tmp_path in observer._roots
        observer.release(tmp_path)
        assert tmp_path not in observer._roots
        assert commands[-1][0] == 'watch-del'
    finally:
        observer.close()


def test_canceled_native_watch_is_reinstalled_instead_of_permanent_full_fallback(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_change_journal as changes

    monkeypatch.setattr(changes.sys, 'platform', 'linux')
    observer = changes.OwnerChangeJournal()
    observer._start = lambda: None
    canceled = [True]
    watches = []

    def request(command):
        if command[0] == 'watch':
            watches.append(command[1])
            return {'watcher': 'inotify'}
        if command[0] == 'watch-del':
            return {}
        if canceled[0]:
            canceled[0] = False
            raise OSError('root was canceled')
        return {'clock': 'c:recreated:1', 'files': [], 'is_fresh_instance': True}

    observer._request = request
    try:
        assert observer.query(tmp_path, 'c:old:1').reason == 'observer_unavailable'
        assert tmp_path not in observer._roots
        repaired = observer.query(tmp_path, 'c:old:1')
        assert not repaired.complete and repaired.reason == 'observer_fresh_instance'
        assert len(watches) == 2
    finally:
        observer.close()


def test_snapshot_projection_factory_closes_bad_database_connection(tmp_path, monkeypatch):
    import sqlite3

    from agent_py_agent.agent.runtime_db.recovery_guard import runtime_db_generation_guard

    path = tmp_path / 'index.sqlite3'
    path.write_bytes(b'bad sqlite projection')
    opened = []
    original = sqlite3.connect

    def connection(*args, **kwargs):
        conn = original(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, 'connect', connection)
    with pytest.raises(sqlite3.DatabaseError) as retained:
        with OwnerSnapshotIndex.open(path):
            pytest.fail('bad projection was treated as initialized')
    assert retained.value.__traceback__ is not None and opened
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[0].execute('SELECT 1')
    with runtime_db_generation_guard(path, exclusive=True):
        pass


def test_bad_local_projection_is_preserved_then_authoritatively_rebuilt(tmp_path):
    store, warm, home, journal = _case(tmp_path)
    source = (home / 'files/0').read_bytes()
    (warm / 'snapshot-index.sqlite3').write_bytes(b'broken local projection')
    with _checkout(store, warm, home):
        assert (home / 'files/0').read_bytes() == source
    assert list(warm.glob('snapshot-index.sqlite3.damaged-*'))
    with OwnerSnapshotIndex.open(warm / 'snapshot-index.sqlite3') as index:
        assert index.metadata()['recovered_index'] is True
        assert index.metadata()['reason'] == 'cache_projection_unreadable'
    assert json.loads((warm / 'state.json').read_text())['state'] == 'CLEAN'


def test_nested_directory_changes_expand_each_changed_subtree_once(tmp_path):
    store, warm, home, journal = _case(tmp_path, 4)
    original = store._warm_paths
    expanded = []

    def scan(path, **kwargs):
        expanded.append(path)
        return original(path, **kwargs)

    store._warm_paths = scan
    with _checkout(store, warm, home):
        (home / 'files').rename(home / 'moved')
        (home / 'moved/sub/nested').mkdir(parents=True)
        (home / 'moved/sub/nested/new').write_bytes(b'new')
        journal.record('files', exists=False, type='d')
        for directory in ('moved', 'moved/sub', 'moved/sub/nested'):
            journal.record(directory, type='d', new=True)
        journal.record('moved/sub/nested/new', new=True)
    assert expanded == [home / 'moved']
    assert 'moved/sub/nested/new' in store._head.rows


def test_oversized_observer_configuration_is_rejected_without_reading_body(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_py_agent.agent import owner_change_journal as changes

    (tmp_path / '.watchmanconfig').write_bytes(b'x' * (1024 * 1024))
    monkeypatch.setattr(changes.sys, 'platform', 'linux')
    monkeypatch.setattr(Path, 'read_bytes', lambda *_: pytest.fail('configuration was read without fixed budget'))
    observer = changes.OwnerChangeJournal()
    observer._start = lambda: None
    observer._request = lambda *_: pytest.fail('unverified configuration reached observer')
    try:
        batch = observer.query(tmp_path)
        assert not batch.complete and batch.reason == 'observer_configuration_conflict'
    finally:
        observer.close()


def test_handler_lifecycle_closes_after_normal_and_failed_execution():
    from types import SimpleNamespace

    from agent_py_agent.agent.worker_entry import handler_lifecycle

    events = []
    handler = SimpleNamespace(startup=lambda: events.append('start'), close=lambda: events.append('close'))
    with handler_lifecycle(handler):
        events.append('body')
    assert events == ['start', 'body', 'close']
    with pytest.raises(RuntimeError, match='handler failed'):
        with handler_lifecycle(handler):
            raise RuntimeError('handler failed')
    assert events[-2:] == ['start', 'close']


def test_handler_startup_failure_still_closes_and_preserves_primary_cleanup_error():
    from types import SimpleNamespace

    from agent_py_agent.agent.worker_entry import handler_lifecycle

    primary, cleanup = RuntimeError('startup failed'), OSError('cleanup failed')

    def startup():
        raise primary

    def close():
        raise cleanup

    with pytest.raises(RuntimeError) as held:
        with handler_lifecycle(SimpleNamespace(startup=startup, close=close)):
            pytest.fail('failed startup entered handler body')
    assert held.value is primary and primary.handler_cleanup_error is cleanup
