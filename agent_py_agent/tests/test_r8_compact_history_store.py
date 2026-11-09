"""R8：持久历史根、分叉、崩溃搬迁与冷启动定位的运行时回归。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import compact_checkpoint_scan as scan
from agent_py_agent.agent.conversation.immutable_node_store import ImmutableNodeStore
from agent_py_agent.agent.conversation.store_io import ImmutableJsonIndex
from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope


def _drive_compact_lane(case, *, predicate=lambda: True, cursor=None, **kwargs):
    from agent_py_agent.agent.conversation.compact_reachability import (
        maintain_compact_reachability_lane,
    )
    from agent_py_agent.tests.test_issues_w3_compact_reachability import home_for, request_for

    cursor = cursor or {}
    for _ in range(128):
        result = maintain_compact_reachability_lane(request_for(home_for(case), cursor=cursor, **kwargs))
        cursor = result['cursor']
        if result['complete'] and predicate():
            return result
    raise AssertionError(f'bounded maintenance slices did not converge: {result}')

def test_ten_thousand_checkpoint_roots_use_constant_inodes_and_keep_branches(tmp_path):
    index = ImmutableJsonIndex(tmp_path, ('nodes',), durable_nodes=True)
    store = ImmutableNodeStore(tmp_path, ('nodes',))
    snapshots = []
    reference = None
    with sqlite_connection_scope(enabled=True), store.batch():
        for generation in range(1, 10001):
            # 每一代都是可单独恢复的 checkpoint pin；每隔一代从旧根分叉。
            base = reference if generation % 2 else (snapshots[generation // 2 - 1] if snapshots else None)
            reference = index.put(base, 'value', generation)
            store.pin(f'checkpoint-{generation}', generation, [reference], committed=True)
            snapshots.append(reference)
    assert len(list((tmp_path / 'nodes').iterdir())) <= 6
    assert not list((tmp_path / 'nodes').glob('*.json'))
    for generation in (1, 2, 239, 240, 241, 4999, 9999, 10000):
        assert index.get(snapshots[generation - 1], 'value') == generation
    with store.connection() as conn:
        assert conn.execute('SELECT count(*) FROM nodes').fetchone()[0] == 10000
        assert conn.execute('SELECT count(*) FROM pins WHERE committed=1').fetchone()[0] == 10000
    # 历史 roots 数超过旧mark额度也不影响事务仓零引用队列。
    assert store.collect(generation=10000, cutoff=time.time() + 864000, limit=1) == (0, 0, 0)
    assert index.get(snapshots[0], 'value') == 1


def test_parent_edges_and_committed_pins_protect_shared_nodes_and_failed_candidates(tmp_path):
    index = ImmutableJsonIndex(tmp_path, ('nodes',), durable_nodes=True)
    store = ImmutableNodeStore(tmp_path, ('nodes',))
    first = index.put_absent(None, ((str(i), i) for i in range(64)))
    winning = index.put(first, 'later', 'winner')
    losing = index.put(first, 'later', 'loser')
    store.pin('first', 1, [first], committed=True)
    store.pin('winner', 2, [winning], committed=True)
    store.pin('loser', 2, [losing])
    future = index.put(winning, 'future', True)
    store.pin('future', 3, [future])
    for _ in range(100):
        used, _, _ = store.collect(generation=2, cutoff=time.time() + 864000, limit=7)
        if not used:
            break
    assert index.get(first, 'later') is None
    assert index.get(winning, 'later') == 'winner'
    assert index.get(future, 'future') is True
    with pytest.raises(FileNotFoundError):
        index._load(losing)
    with store.connection() as conn:
        assert conn.execute('SELECT 1 FROM pins WHERE checkpoint="future"').fetchone()
        assert not conn.execute('SELECT 1 FROM pins WHERE checkpoint="loser"').fetchone()


def test_cold_offset_projection_survives_process_restart_and_window_eviction(tmp_path, monkeypatch):
    path = tmp_path / 'checkpoints.jsonl'
    path.write_bytes(b''.join(json.dumps({'checkpoint_id': str(i), 'payload': 'x' * 64}).encode() + b'\n'
                              for i in range(5000)))
    monkeypatch.setattr(scan, '_MAX_CACHED_OFFSETS', 2)
    with scan.checkpoint_row_lookup(path, publish_projection=True) as lookup:
        assert lookup('0')['checkpoint_id'] == '0'
    scan._CHECKPOINT_OFFSETS.clear()
    code = '''
import sys
from pathlib import Path
from agent_py_agent.agent.conversation import compact_checkpoint_scan as scan
def forbidden(*args):
    raise AssertionError('cold restart rescanned source history')
scan._checkpoint_byte_rows = forbidden
with scan.checkpoint_row_lookup(Path(sys.argv[1])) as lookup:
    assert lookup('0')['checkpoint_id'] == '0'
    assert lookup('4999')['checkpoint_id'] == '4999'
    assert lookup('missing') is None
'''
    isolated = Path(os.environ.get('R8_COMPACT_TEST_HOME', str(tmp_path / 'subprocess-home')))
    isolated.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'HOME': str(isolated), 'MY_AGENT_HOME': str(isolated)}
    subprocess.run([sys.executable, '-c', code, str(path)], cwd=Path(__file__).resolve().parents[2],
                   env=env, check=True, timeout=30)


def test_persisted_offset_does_not_hide_new_bad_validator_rows_or_source_replacement(tmp_path):
    path = tmp_path / 'checkpoints.jsonl'
    path.write_bytes(b'{"checkpoint_id":"old"}\n')
    with scan.checkpoint_row_lookup(path, publish_projection=True):
        pass
    with path.open('ab') as handle:
        handle.write(b'{"checkpoint_id":"bad"}\n')

    def strict(row):
        if row['checkpoint_id'] == 'bad':
            raise OSError('bad orphan remains fatal')

    with pytest.raises(OSError, match='bad orphan remains fatal'):
        with scan.checkpoint_row_lookup(path, validator=strict):
            pass
    # 失败事务不能把只扫一半的 index 发布成完整水位。
    path.write_bytes(b'{"checkpoint_id":"replacement"}\n')
    scan._CHECKPOINT_OFFSETS.clear()
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('old') is None
        assert lookup('replacement') is not None


def test_index_address_tamper_is_not_returned_as_a_different_checkpoint(tmp_path):
    from agent_py_agent.agent.conversation.checkpoint_offset_store import CheckpointOffsetStore

    path = tmp_path / 'checkpoints.jsonl'
    rows = [b'{"checkpoint_id":"one"}\n', b'{"checkpoint_id":"two"}\n']
    path.write_bytes(b''.join(rows))
    with scan.checkpoint_row_lookup(path, publish_projection=True):
        pass
    projection = CheckpointOffsetStore(path)
    with projection.connection() as conn:
        conn.execute('UPDATE offsets SET offset=?,length=?,digest=? WHERE identity="one"',
                     (999, len(rows[1]), hashlib.sha256(rows[1]).digest()))
        conn.commit()
    scan._CHECKPOINT_OFFSETS.clear()
    with scan.checkpoint_row_lookup(path) as lookup:
        with pytest.raises(OSError, match='address is invalid'):
            lookup('one')


def test_legacy_migration_crash_keeps_source_and_resumes_without_full_mark(compact_case, monkeypatch):
    from agent_py_agent.agent.conversation import compact_reachability
    from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE, CompactScope
    from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
    from agent_py_agent.tests.test_issues_w3_compact_reachability import (
        committed_pair,
        home_for,
        request_for,
    )

    case = compact_case
    first, head = committed_pair(case)
    root = case.root / 'compact'
    stores = [ImmutableNodeStore(root, ('evidence', case.thread_id, name)) for name in ('indexes', 'timeline')]
    legacy_paths = []
    for store in stores:
        with store.connection() as conn:
            rows = conn.execute('SELECT name,raw FROM nodes WHERE raw IS NOT NULL').fetchall()
        for name, raw in rows:
            path = store.path.parent / name
            path.write_bytes(raw)
            legacy_paths.append(path)
        # 模拟旧版本只存在内容寻址文件，原 checkpoint seal 原封保留。
        for suffix in ('', '-wal', '-shm'):
            store.path.with_name(store.path.name + suffix).unlink(missing_ok=True)
    stores[0].set_state('sealed_head', '')
    original = ImmutableNodeStore.save
    interrupted = []

    def crash_after_copy(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if not interrupted:
            interrupted.append(True)
            raise OSError('simulated migration crash after durable copy')

    def forbidden_mark(*args, **kwargs):
        raise AssertionError('new store must not mark all historical roots')

    monkeypatch.setattr(compact_reachability, 'mark_reachable_nodes', forbidden_mark)
    monkeypatch.setattr(ImmutableNodeStore, 'save', crash_after_copy)
    request = request_for(home_for(case))
    result = compact_reachability.maintain_compact_reachability_lane(request)
    assert result['errors'] and interrupted, json.dumps(result)
    assert all(path.exists() for path in legacy_paths)
    monkeypatch.setattr(ImmutableNodeStore, 'save', original)
    result = _drive_compact_lane(case, cursor=result['cursor'], items=3,
                                 predicate=lambda: not any(path.exists() for path in legacy_paths))
    assert result['complete'] and not result['errors']
    assert not any(path.exists() for path in legacy_paths)
    assert resolve_compact_summary_view(case.agent, first, THREAD_COMPACT_SCOPE).summary == '第一代'
    assert resolve_compact_summary_view(case.agent, head, THREAD_COMPACT_SCOPE).summary == '第二代'
    task = CompactScope(kind='task', task_id='old-task', task_ids=('old-task',), created_at=first.compact_updated_at)
    assert resolve_compact_summary_view(case.agent, head, task).summary == '第一代'


def test_cas_commit_pin_failure_is_pending_and_gc_recovers_before_releasing_candidates(compact_case, monkeypatch):
    from agent_py_agent.agent.conversation import compact_index_lifecycle, compact_reachability
    from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
    from agent_py_agent.tests.test_compact_scoped_checkpoint import _commit_transcript
    from agent_py_agent.tests.test_issues_w3_compact_reachability import home_for, request_for

    case = compact_case
    original = compact_index_lifecycle.commit_index_checkpoint

    def fault(*args):
        raise OSError('pin publication interrupted after thread CAS')

    monkeypatch.setattr(compact_index_lifecycle, 'commit_index_checkpoint', fault)
    head, _, identity = _commit_transcript(case, scope=THREAD_COMPACT_SCOPE, summary='committed', content='m', publish=True)
    assert head.compact_checkpoint_id == identity
    assert head.metadata['compact_index_maintenance']['status'] == 'pending'
    monkeypatch.setattr(compact_index_lifecycle, 'commit_index_checkpoint', original)
    result = _drive_compact_lane(case, predicate=lambda: ImmutableNodeStore(
        case.root / 'compact', ('evidence', case.thread_id, 'indexes')).state('sealed_head') == identity)
    assert result['complete'] and not result['errors']
    store = ImmutableNodeStore(case.root / 'compact', ('evidence', case.thread_id, 'indexes'))
    assert store.state('sealed_head') == identity
    with store.connection() as conn:
        assert conn.execute('SELECT count(*) FROM pins WHERE checkpoint=? AND committed=1', (identity,)).fetchone()[0] > 0


from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as _compact_case

compact_case = _compact_case


def test_structurally_bad_projection_is_retired_only_after_strict_source_scan(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.checkpoint_offset_store import CheckpointOffsetStore

    path = tmp_path / 'ledger.jsonl'
    path.write_bytes(b'{"checkpoint_id":"one"}\n')
    with scan.checkpoint_row_lookup(path, publish_projection=True):
        pass
    projection = CheckpointOffsetStore(path)
    projection.path.write_bytes(b'not a sqlite projection')
    scan._CHECKPOINT_OFFSETS.clear()
    with scan.checkpoint_row_lookup(path, publish_projection=True) as lookup:
        assert lookup('one')['checkpoint_id'] == 'one'
    assert not projection.path.exists()
    assert list(tmp_path.glob('*.damaged-*'))
    with scan.checkpoint_row_lookup(path, publish_projection=True):
        pass
    scan._CHECKPOINT_OFFSETS.clear()

    def forbidden(*args):
        raise AssertionError('repaired persistent projection rescanned source')

    monkeypatch.setattr(scan, '_checkpoint_byte_rows', forbidden)
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('one')['checkpoint_id'] == 'one'


def test_collected_unpublished_node_cannot_be_pinned_or_linked_later(tmp_path):
    index = ImmutableJsonIndex(tmp_path, ('nodes',), durable_nodes=True)
    store = ImmutableNodeStore(tmp_path, ('nodes',))
    unpublished = index.put(None, 'value', 'not committed')
    assert store.collect(generation=0, cutoff=time.time() + 864000, limit=10)[1] == 1
    with pytest.raises(FileNotFoundError, match='before publication'):
        store.pin('late-candidate', 1, [unpublished])
    with pytest.raises(FileNotFoundError, match='before publication'):
        index._save({'children': {'0': unpublished}})
    with store.connection() as conn:
        assert conn.execute('SELECT count(*) FROM pins').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM nodes').fetchone()[0] == 0


def test_object_gc_rechecks_pin_in_transaction_without_enumerating_history(compact_case, monkeypatch):
    from agent_py_agent.agent.conversation import compact_checkpoint, compact_reachability
    from agent_py_agent.agent.conversation.hamt_reachability import _Directory
    from agent_py_agent.tests.test_issues_w3_compact_reachability import (
        committed_pair,
        home_for,
        request_for,
    )

    case = compact_case
    _, head = committed_pair(case)
    root = case.root / 'compact'
    reference = compact_checkpoint._write_cold_payload(root, case.thread_id, {'uncommitted': True})
    path = root / 'evidence' / case.thread_id / reference['ref']
    os.utime(path, (0, 0))
    store = ImmutableNodeStore(root, ('evidence', case.thread_id, 'indexes'))
    with store.connection(write=True) as conn:
        conn.execute('UPDATE files SET touched=0 WHERE namespace="payload" AND name=?', (reference['ref'],))
    names = _Directory.names

    def bounded_names(self):
        if self.path in (root / 'evidence' / case.thread_id, root / 'archive' / case.thread_id):
            raise AssertionError('steady GC enumerated checkpoint/payload history')
        yield from names(self)

    raced = []

    def commit(operation):
        if operation.__name__ == 'erase' and not raced:
            store.pin('racing-candidate', head.compact_generation + 1, [], objects=[('payload', path.name)],
                      object_validator=lambda _namespace, _name: path.stat())
            raced.append(True)
        return operation()

    monkeypatch.setattr(_Directory, 'names', bounded_names)
    result = _drive_compact_lane(case, commit=commit, predicate=lambda: bool(raced))
    assert result['complete'] and not result['errors'] and raced
    assert path.exists()
    assert result['deleted_items'] == 0


def test_unchanged_cold_projection_uses_only_read_only_connection(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.checkpoint_offset_store import CheckpointOffsetStore

    path = tmp_path / 'ledger.jsonl'
    path.write_bytes(b'{"checkpoint_id":"one"}\n')
    with scan.checkpoint_row_lookup(path, publish_projection=True):
        pass
    projection = CheckpointOffsetStore(path)
    before = projection.path.stat().st_mtime_ns
    scan._CHECKPOINT_OFFSETS.clear()
    original = CheckpointOffsetStore.connection

    def read_only(self, *, write=True):
        assert not write, 'unchanged cold lookup attempted projection mutation'
        return original(self, write=False)

    monkeypatch.setattr(CheckpointOffsetStore, 'connection', read_only)
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('one')['checkpoint_id'] == 'one'
    assert projection.path.stat().st_mtime_ns == before


@pytest.mark.parametrize('kind', ['nodes', 'offsets'])
def test_bad_database_initialization_closes_physical_connection_while_error_is_held(tmp_path, monkeypatch, kind):
    import sqlite3

    from agent_py_agent.agent.conversation.checkpoint_offset_store import CheckpointOffsetStore
    from agent_py_agent.agent.runtime_db.recovery_guard import runtime_db_generation_guard

    store = (ImmutableNodeStore(tmp_path, ('nodes',)) if kind == 'nodes'
             else CheckpointOffsetStore(tmp_path / 'ledger.jsonl'))
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b'not a sqlite database')
    physical = []
    original = sqlite3.connect

    def connected(*args, **kwargs):
        conn = original(*args, **kwargs)
        physical.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, 'connect', connected)
    with pytest.raises(sqlite3.DatabaseError) as retained:
        with store.connection(write=True):
            pass
    assert retained.value.__traceback__ is not None and physical
    for conn in physical:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            conn.execute('SELECT 1')
    # 原异常与raw connection仍有强引用，物理关闭证明成立后 EX 才可取得。
    with runtime_db_generation_guard(store.path, exclusive=True):
        assert retained.value.__traceback__ is not None


@pytest.mark.parametrize('kind', ['nodes', 'offsets'])
def test_initialization_close_failure_retains_generation_until_creator_retires(tmp_path, monkeypatch, kind):
    import sqlite3
    from threading import Event, Thread

    from agent_py_agent.agent.conversation import checkpoint_offset_store, immutable_node_store
    from agent_py_agent.agent.runtime_db.recovery_guard import (
        close_runtime_connection,
        runtime_db_generation_guard,
    )

    class FirstCloseFails(sqlite3.Connection):
        closes = 0

        def close(self):
            self.closes += 1
            if self.closes == 1:
                raise sqlite3.OperationalError('physical close deferred')
            return super().close()

    module = immutable_node_store if kind == 'nodes' else checkpoint_offset_store
    monkeypatch.setattr(module, 'sqlite_connection_factory', lambda: FirstCloseFails)
    store = (ImmutableNodeStore(tmp_path, ('nodes',)) if kind == 'nodes'
             else checkpoint_offset_store.CheckpointOffsetStore(tmp_path / 'ledger.jsonl'))
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b'not a sqlite database')
    failed, release = Event(), Event()
    retained = []

    def creator():
        try:
            with store.connection(write=True):
                raise AssertionError('bad initialization must fail')
        except sqlite3.DatabaseError as error:
            retained.append(error)
            failed.set()
            assert release.wait(10)
            close_runtime_connection(error.sqlite_unclosed_factory_connection)

    thread = Thread(target=creator)
    thread.start()
    assert failed.wait(10)
    try:
        assert isinstance(retained[0].sqlite_cleanup_error, sqlite3.OperationalError)
        with pytest.raises(sqlite3.OperationalError):
            with runtime_db_generation_guard(store.path, exclusive=True):
                raise AssertionError('physical connection still holds original generation')
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    with runtime_db_generation_guard(store.path, exclusive=True):
        assert retained[0].__traceback__ is not None


def test_node_batch_is_atomic_across_instances_and_cannot_be_revived(tmp_path):
    from contextvars import copy_context

    index = ImmutableJsonIndex(tmp_path, ('nodes',), durable_nodes=True)
    store = ImmutableNodeStore(tmp_path, ('nodes',))
    traces = []
    with store.batch():
        with store.connection(write=True) as conn:
            conn.set_trace_callback(traces.append)
        first = index.put(None, 'first', 1)
        with ImmutableNodeStore(tmp_path, ('nodes',)).batch():
            second = index.put(first, 'second', 2)
            store.pin('checkpoint', 1, [second], committed=True)
        assert index.get(second, 'first') == 1 and index.get(second, 'second') == 2
        retained = copy_context()
    assert sum(statement == 'COMMIT' for statement in traces) == 1
    assert index.get(second, 'first') == 1
    with pytest.raises(OSError, match='no longer owned'):
        retained.run(index.get, second, 'first')
    with pytest.raises(RuntimeError, match='abort logical root'):
        with index.write_batch():
            rejected = index.put(second, 'never committed', True)
            raise RuntimeError('abort logical root')
    assert store.read(rejected['ref']['ref']) is None
    assert index.get(second, 'never committed') is None


def test_node_batch_refuses_copied_context_on_another_thread(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    index = ImmutableJsonIndex(tmp_path, ('nodes',), durable_nodes=True)
    with index.write_batch():
        reference = index.put(None, 'key', True)
        inherited = copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(inherited.run, index.get, reference, 'key')
            with pytest.raises(OSError, match='no longer owned'):
                future.result(timeout=5)
        assert index.get(reference, 'key') is True
    assert index.get(reference, 'key') is True


@pytest.mark.parametrize('history', [1, 240, 10000])
def test_persistent_node_read_write_connections_do_not_grow_with_history(tmp_path, monkeypatch, history):
    import sqlite3

    index = ImmutableJsonIndex(tmp_path, ('nodes',), durable_nodes=True)
    store = ImmutableNodeStore(tmp_path, ('nodes',))
    first, reference = None, None
    with store.batch():
        for generation in range(history):
            reference = index.put(reference, 'value', generation)
            first = first or reference
            store.pin(str(generation), generation, [reference], committed=True)
    original, opened = sqlite3.connect, []

    def connect(*args, **kwargs):
        conn = original(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, 'connect', connect)
    with sqlite_connection_scope(enabled=True):
        for _ in range(4):
            assert index.get(first, 'value') == 0
            assert index.get(reference, 'value') == history - 1
            store.pin('new-candidate', history + 1, [reference])
        assert len(opened) == 2  # 独立 ro/rw，不把写权限借给只读调用。
        assert not any(conn.in_transaction for conn in opened)
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')


def test_read_only_checkpoint_lookup_never_births_or_repairs_projection(tmp_path):
    from agent_py_agent.agent.conversation.checkpoint_offset_store import CheckpointOffsetStore

    path = tmp_path / 'ledger.jsonl'
    path.write_bytes(b'{"checkpoint_id":"one"}\n')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('one')['checkpoint_id'] == 'one'
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    with scan.checkpoint_row_lookup(path, publish_projection=True):
        pass
    projection = CheckpointOffsetStore(path)
    projection.path.write_bytes(b'bad sqlite content')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with scan.checkpoint_row_lookup(path) as lookup:
        assert lookup('one')['checkpoint_id'] == 'one'
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
