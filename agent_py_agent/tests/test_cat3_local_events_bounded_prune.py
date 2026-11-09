"""Cat3-R4：真实 SQLite 前缀裁剪、封存段过期与有界 FTS 对齐。"""
from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from agent_py_agent.agent.common.global_write_commit import global_write_commit_scope
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.local_storage import LocalStore, maintenance
from agent_py_agent.agent.local_storage.events import (
    event_segments_dir,
    rotate_event_jsonl_if_due_unlocked,
)
from agent_py_agent.agent.local_storage.search import FTS_ROWID_ALIGNED_KEY, fts_rowid_aligned
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.storage_lanes import (
    STORAGE_LANE_SLICE_BYTES,
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)


def _seed(store, count, *, timestamp=1):
    with store._connection() as conn:
        conn.executemany("INSERT INTO events(event_id,event_type,created_at) VALUES(?, 'seed', ?)",
            ((f'event-{index:06d}', timestamp) for index in range(count)))
        conn.commit()


def _line(identifier, **extra):
    return json.dumps({'event_id': identifier, **extra}).encode() + b'\n'


def _meta(store, key):
    with store._connection() as conn:
        row = conn.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def _request(home, cursor=None, *, coordinator=False, items=None, size=None):
    return StorageLaneRequest(kind='local_events', home=home, now=time.time() + 60, cursor=cursor or {},
        budget=StorageLaneBudget(items, size, None), legal_hold=False, coordinator=coordinator,
        pressure=None, commit=lambda operation: operation())


def test_seq_head_selection_is_bounded_and_uses_index_endpoints(tmp_path):
    store = LocalStore(tmp_path / 'local.db', enable_fts=False)
    _seed(store, 30_000)
    with store._connection() as conn:
        selected = maintenance._select_prunable_event_ids(conn, 50, 20_000, 5000)
        assert selected == {f'event-{index:06d}' for index in range(5000)}
        for sql, values in [
            ('SELECT seq FROM events ORDER BY seq DESC LIMIT 1 OFFSET ?', (20_000,)),
            ('SELECT event_id, created_at FROM events WHERE seq <= ? ORDER BY seq ASC LIMIT ?', (10_000, 5000)),
        ]:
            plan = conn.execute('EXPLAIN QUERY PLAN ' + sql, values).fetchall()
            assert not any('TEMP B-TREE' in row[3] for row in plan), plan
        conn.execute('UPDATE events SET created_at=100 WHERE seq=3')
        assert maintenance._select_prunable_event_ids(conn, 50, 20_000, 5000) == {'event-000000', 'event-000001'}


def test_every_catchup_write_deletes_at_most_five_thousand_events(tmp_path, monkeypatch):
    home = home_paths(tmp_path / 'home')
    store = LocalStore(home.owner_data_dir / 'local_store' / 'local.db')
    _seed(store, 30_000)
    batches, original = [], maintenance._delete_prunable_events

    def measured(conn, *options):
        before = conn.total_changes
        result = original(conn, *options)
        batches.append(conn.total_changes - before)
        return result

    monkeypatch.setattr(maintenance, '_delete_prunable_events', measured)
    cursor = {}
    for _ in range(40):
        result = validate_lane_receipt(maintenance.maintain_local_events_lane(_request(home, cursor)))
        cursor = result['cursor']
        if result['complete']:
            break
    assert result['complete'], result
    assert sum(batches) == 10_000 and max(batches) <= 5000
    assert _meta(store, 'events_prune_pending_ids') is None


def test_public_prune_also_caps_one_write_transaction(tmp_path):
    store = LocalStore(tmp_path / 'local.db', enable_fts=False)
    _seed(store, 30_000)
    assert store.prune_events(before=50, keep_last=20_000, max_delete=30_000)['deleted_db'] == 5000


def test_expiry_removes_only_sealed_prefix_and_keeps_active_inode(tmp_path):
    store = LocalStore(tmp_path / 'local.db', enable_fts=False)
    _seed(store, 4)
    directory = event_segments_dir(store.events_path)
    directory.mkdir()
    first, second, third = [directory / f'2020-01-0{index}.jsonl' for index in range(1, 4)]
    first.write_bytes(_line('event-000000'))
    second.write_bytes(_line('event-000001'))
    third.write_bytes(_line('event-000002'))
    store.events_path.write_bytes(_line('event-000003'))
    original = [(path.stat().st_ino, path.stat().st_size) for path in (second, third, store.events_path)]
    with store._connection() as conn:
        conn.execute("DELETE FROM events WHERE event_id='event-000000'")
        conn.commit()
    fd_root = '/dev/fd' if os.path.isdir('/dev/fd') else '/proc/self/fd'
    before = len(os.listdir(fd_root))
    result = maintenance._expire_event_segments(store, 64 * 1024)
    assert len(os.listdir(fd_root)) - before == 0
    assert result['removed'] == 0 and result['removed_segments'] == 1
    assert result['removed_bytes'] > 0 and not first.exists()
    assert [(path.stat().st_ino, path.stat().st_size) for path in (second, third, store.events_path)] == original


def test_expiry_obeys_tail_read_budget(tmp_path):
    store = LocalStore(tmp_path / 'local.db', enable_fts=False)
    directory = event_segments_dir(store.events_path)
    directory.mkdir()
    segment = directory / '2020-01-01.jsonl'
    segment.write_bytes(_line('expired', padding='x' * 1000))
    result = maintenance._expire_event_segments(store, 1)
    assert not result['complete'] and result['scanned_bytes'] <= 1
    assert segment.exists()


def test_lane_can_expire_a_segment_with_exactly_one_tail_window_budget(tmp_path):
    home = home_paths(tmp_path / 'home')
    store = LocalStore(home.owner_data_dir / 'local_store' / 'local.db', enable_fts=False)
    directory = event_segments_dir(store.events_path)
    directory.mkdir()
    segment = directory / '2020-01-01.jsonl'
    window = 64 * 1024
    segment.write_bytes(_line('expired', padding='x' * (window - len(_line('expired', padding='')))))
    store.events_path.write_bytes(_line('untouched-active'))
    result = validate_lane_receipt(maintenance.maintain_local_events_lane(_request(home, size=window)))
    assert not segment.exists(), result
    assert result['bytes'] == window
    assert store.events_path.read_bytes() == _line('untouched-active')
    # 保留着活跃事件的封存段只读检查一次即可确认空闲，不能永久卡住完成回执。
    store.record_event('live')
    with store._connection() as conn:
        live_id = conn.execute('SELECT event_id FROM events ORDER BY seq DESC LIMIT 1').fetchone()[0]
    segment.write_bytes(_line(live_id))
    result = validate_lane_receipt(maintenance.maintain_local_events_lane(_request(home)))
    assert segment.exists() and result['complete'], result


@pytest.mark.parametrize('tail', [b'broken\n', b'{"event_id":', b'{}\n'])
def test_bad_segment_tail_is_retained_with_structured_error(tmp_path, tail):
    store = LocalStore(tmp_path / 'local.db', enable_fts=False)
    directory = event_segments_dir(store.events_path)
    directory.mkdir()
    segment = directory / '2020-01-01.jsonl'
    segment.write_bytes(tail)
    result = store.prune_events_step(before=50)
    assert segment.read_bytes() == tail and result['errors']
    assert isinstance(result['errors'][0]['code'], str)
    assert result['deleted_jsonl'] == 0


def test_legacy_active_file_expires_only_after_real_rotation(tmp_path):
    store = LocalStore(tmp_path / 'local.db', enable_fts=False)
    line = _line('legacy-event', created_at='2020-01-01T00:00:00Z', padding='x' * 1000)
    store.events_path.write_bytes(line * (STORAGE_LANE_SLICE_BYTES // len(line) + 1))
    assert maintenance._expire_event_segments(store, 64 * 1024)['removed_segments'] == 0
    with locked_json_path(store.events_path):
        segment = rotate_event_jsonl_if_due_unlocked(store.events_path)
    assert segment is not None
    store.record_event('current')
    active = store.events_path.read_bytes()
    result = maintenance._expire_event_segments(store, 64 * 1024)
    assert result['removed_segments'] == 1 and not segment.exists()
    assert store.events_path.read_bytes() == active


def test_legacy_pending_cursor_and_only_its_literal_temp_are_consumed(tmp_path):
    store = LocalStore(tmp_path / 'local.db', enable_fts=False)
    temporary = tmp_path / '.events-prune-legacy.step'
    unrelated = tmp_path / '.events-prune-unrelated.step'
    for path in (temporary, unrelated):
        path.write_bytes(b'old rewrite')
        path.chmod(0o600)
    with store._connection() as conn:
        conn.execute("INSERT INTO metadata VALUES('events_prune_pending_ids', ?)", (json.dumps(['old']),))
        conn.execute("INSERT INTO metadata VALUES('events_prune_cursor', ?)",
            (json.dumps({'temporary': temporary.name}),))
        conn.commit()
    store.prune_events_step(before=50)
    assert _meta(store, 'events_prune_pending_ids') is _meta(store, 'events_prune_cursor') is None
    assert not temporary.exists() and unrelated.exists()


@pytest.mark.parametrize('coordinator', [False, True])
def test_lane_aligns_existing_fts_in_multiple_bounded_steps(tmp_path, coordinator):
    home = home_paths(tmp_path / 'home')
    store = LocalStore(home.owner_data_dir / 'local_store' / 'local.db')
    for index in range(10):
        store.upsert_record(source_type='memory', source_id=str(index), title=str(index), content='searchable body')
    with store._connection() as conn:
        conn.execute('UPDATE records SET rowid=rowid+100')
        conn.execute('INSERT OR REPLACE INTO metadata VALUES(?, ?)', (FTS_ROWID_ALIGNED_KEY, 'false'))
        conn.commit()
        assert not fts_rowid_aligned(conn)
    from agent_py_agent.agent.local_storage.fts_upgrade import (
        advance_upgrade,
        fts_upgrade_tick_scope,
    )

    previous, progress = {}, []
    with global_write_commit_scope(lambda operation: operation()):
        for turn in range(100):
            with fts_upgrade_tick_scope(deadline=time.monotonic() + 20, max_steps=1):
                result = advance_upgrade(home.owner_home_dir, previous, 1000 + turn * 61,
                    commit=(lambda operation: operation()) if coordinator else None)
            previous = {'local_store_fts_upgrade': result}
            progress.append(result)
            if _meta(store, FTS_ROWID_ALIGNED_KEY) == 'true':
                break
    assert _meta(store, FTS_ROWID_ALIGNED_KEY) == 'true', result
    assert progress[0]['status'] == 'partial' and progress[0]['copied'] <= 128
    assert any(item['status'] == 'complete' for item in progress)
    with store._connection() as conn:
        assert fts_rowid_aligned(conn)
        rows = conn.execute('SELECT r.rowid,f.rowid FROM records r JOIN records_fts f ON r.id=f.id').fetchall()
        assert len(rows) == 10 and all(left == right for left, right in rows)


def test_vacuum_conversion_invalidates_existing_alignment(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE ballast(body BLOB)')
    store = LocalStore(path)
    store.upsert_record(source_type='memory', source_id='one', title='one', content='one body')
    assert _meta(store, FTS_ROWID_ALIGNED_KEY) == 'true'
    with store._connection() as conn:
        conn.executemany('INSERT INTO ballast VALUES(?)', [(b'x' * 4000,)] * 100)
        conn.execute('DELETE FROM ballast')
        conn.commit()
    result = store.incremental_vacuum_step(max_pages=1, allow_convert=True)
    assert result['mode'] == 'converted', result
    assert _meta(store, FTS_ROWID_ALIGNED_KEY) == 'false'


def test_missing_segment_directory_does_not_block_sql_progress(tmp_path):
    store = LocalStore(tmp_path / 'local.db', enable_fts=False)
    _seed(store, 4)
    store.events_path.write_bytes(_line('untouched-active'))
    result = store.prune_events_step(before=50, keep_last=1)
    assert result['deleted_db'] == 3 and result['deleted_jsonl'] == 0
    assert store.events_path.read_bytes() == _line('untouched-active')
    assert not event_segments_dir(store.events_path).exists()
