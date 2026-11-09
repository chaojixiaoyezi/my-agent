"""真实旧FTS跨冷构造及原owner维护升级，不借LIKE兜底或手工盖对齐标记。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.local_storage import LocalStore, maintenance
from agent_py_agent.agent.local_storage.search import FTS_ROWID_ALIGNED_KEY
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due


def _legacy(tmp_path):
    home = ensure_my_agent_home(tmp_path)
    store = LocalStore(home.owner_data_dir / 'local_store' / 'local.db')
    items = [store.upsert_record(source_type='memory', source_id=str(i), title=str(i),
                                content=f'legacy searchable needle{i}') for i in range(4)]
    with store._connection() as conn:
        conn.execute('UPDATE records SET rowid=rowid+100')
        conn.execute('DELETE FROM metadata WHERE key IN (?, ?)',
                     (FTS_ROWID_ALIGNED_KEY, 'records_fts_rebuild_pending'))
        conn.commit()
    return home, store, items


def _aligned(store):
    with store._connection() as conn:
        row = conn.execute('SELECT value FROM metadata WHERE key=?', (FTS_ROWID_ALIGNED_KEY,)).fetchone()
    return row is not None and row[0] == 'true'


def test_constructor_preserves_old_active_without_synchronous_rebuild(tmp_path, monkeypatch):
    home, store, items = _legacy(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('constructing a store must not synchronously rebuild history')
    result = store.rebuild_fts_step(max_rows=1, max_records=1)
    assert result['status'] == 'partial'
    monkeypatch.setattr(LocalStore, 'rebuild_fts', forbidden)
    for _ in range(2):
        reopened = LocalStore(store.db_path)
        assert {row.id for row in reopened._search_fts('searchable', limit=10,
                source_type=None, visibility=None)} == {item.id for item in items}
        assert not _aligned(reopened)


@pytest.mark.parametrize('coordinator', [False, True])
@pytest.mark.parametrize('policy_change', [{'maintenance_enabled': False}, {'maintenance_interval_seconds': 0}])
def test_formal_owner_entry_aligns_legacy_despite_disabled_retention(tmp_path, monkeypatch, coordinator, policy_change):
    home, store, items = _legacy(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text())
    policy.update(policy_change)
    home.owner_retention_json.write_text(json.dumps(policy))
    def forbidden(*args, **kwargs):
        pytest.fail('disabled retention must not delete or vacuum')
    monkeypatch.setattr(LocalStore, 'prune_events_step', forbidden)
    monkeypatch.setattr(LocalStore, 'incremental_vacuum_step', forbidden)
    monkeypatch.setattr(LocalStore, 'rebuild_fts', forbidden)
    options = {'commit_transition': lambda operation: operation(), 'include_global_indexes': False} if coordinator else {}
    for turn in range(30):
        run_owner_retention_if_due(home, now=1000 + turn * 61, **options)
        # 每次重新构造，不能靠进程内缓存维持迁移进度。
        store = LocalStore(store.db_path)
        if _aligned(store):
            break
    assert _aligned(store)
    with store._connection() as conn:
        rows = conn.execute('SELECT r.rowid,f.rowid FROM records r JOIN records_fts f ON r.id=f.id').fetchall()
    assert len(rows) == len(items) and all(a == b for a, b in rows)
    assert {r.id for r in store._search_fts('searchable', limit=10, source_type=None, visibility=None)} == {r.id for r in items}
    # 后继重开及更新只允许点删；不会再次启动全历史重建。
    monkeypatch.setattr(LocalStore, 'rebuild_fts_step', forbidden)
    store.upsert_record(source_type='memory', source_id='0', title='updated', content='replacementneedle')
    assert {r.id for r in store._search_fts('replacementneedle', limit=10, source_type=None, visibility=None)} == {items[0].id}
    assert not store._search_fts('needle0', limit=10, source_type=None, visibility=None)


def test_insufficient_space_preserves_active_and_retries_original_job(tmp_path, monkeypatch):
    home, store, items = _legacy(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    actual_os = maintenance.os
    proxy = SimpleNamespace(**vars(actual_os))
    proxy.statvfs = lambda path: SimpleNamespace(f_bavail=0, f_frsize=4096)
    monkeypatch.setattr(maintenance, 'os', proxy)
    run_owner_retention_if_due(home, now=1000)
    state = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
    assert state['local_store_fts_upgrade']['status'] == 'deferred'
    assert not _aligned(store)
    assert {r.id for r in store._search_fts('searchable', limit=10, source_type=None, visibility=None)} == {r.id for r in items}
    monkeypatch.setattr(maintenance, 'os', actual_os)
    for turn in range(30):
        run_owner_retention_if_due(home, now=1061 + turn * 61)
        if _aligned(store):
            break
    assert _aligned(store)


def test_large_document_reuses_original_bounded_single_document_cap(tmp_path):
    home, store, items = _legacy(tmp_path)
    text = 'full large document needle ' * 370_000 + ' deependingneedle'
    large = store.upsert_record(source_type='memory', source_id='large', title='large', content=text)
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    observed = []
    for turn in range(40):
        run_owner_retention_if_due(home, now=1000 + turn * 61)
        state = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
        observed.append(state['local_store_fts_upgrade'])
        if _aligned(store):
            break
    assert any(row.get('single_document_bytes', 0) > 8 * 1024 * 1024 for row in observed)
    assert _aligned(store)
    assert {r.id for r in store._search_fts('deependingneedle', limit=10, source_type=None, visibility=None)} == {large.id}
    assert store.get_record(large.id).content == text


def test_source_rejection_preserves_original_identity_and_no_completion(tmp_path):
    from agent_py_agent.agent.local_storage.fts_upgrade import advance_upgrade

    home, store, items = _legacy(tmp_path)
    original = PermissionError('original source revoked')
    def reject(operation):
        raise original
    with pytest.raises(PermissionError) as caught:
        advance_upgrade(home.owner_home_dir, {}, 1000, commit=reject)
    assert caught.value is original
    assert not _aligned(store)
    assert {r.id for r in store._search_fts('searchable', limit=10, source_type=None, visibility=None)} == {r.id for r in items}


def test_fts_disabled_configuration_does_not_mutate_existing_index(tmp_path, monkeypatch):
    home, store, _ = _legacy(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    def forbidden(*args, **kwargs):
        pytest.fail('disabled FTS must not start an index job')
    monkeypatch.setattr(LocalStore, 'rebuild_fts_step', forbidden)
    run_owner_retention_if_due(home, now=1000, config=SimpleNamespace(local_store_fts_enabled=False))
    assert not _aligned(store)
    with store._connection() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_job'").fetchone() is None


@pytest.mark.parametrize('size', [1000, 20000])
def test_aligned_point_write_work_does_not_scale_with_fts_history(tmp_path, size):
    store = LocalStore(tmp_path / 'scale.db')
    item = store.upsert_record(source_type='memory', source_id='target', title='target', content='oldneedle')
    with store._connection() as conn:
        # 这里只构造检索投影的历史规模，度量原单文档FTS writer，正文迁移另由正式入口测试验证。
        conn.executemany('INSERT INTO records_fts(rowid,id,title,content) VALUES(?,?,?,?)',
            ((i + 2, f'history-{i}', 'history', 'searchable historical body') for i in range(size)))
        conn.commit()
        steps = [0]
        def progress():
            steps[0] += 1
            return 0
        conn.set_progress_handler(progress, 1)
        store._replace_fts_row_at(conn, 1, item.id, 'target', 'replacementneedle')
        conn.set_progress_handler(None, 0)
        conn.commit()
    assert steps[0] < 1000
    assert {r.id for r in store._search_fts('replacementneedle', limit=10, source_type=None, visibility=None)} == {item.id}
    assert not store._search_fts('oldneedle', limit=10, source_type=None, visibility=None)


def test_legal_hold_keeps_deletion_disabled_but_index_progresses(tmp_path, monkeypatch):
    home, store, _ = _legacy(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text())
    policy['legal_hold'] = True
    home.owner_retention_json.write_text(json.dumps(policy))
    def forbidden(*args, **kwargs):
        pytest.fail('hold must preserve event retention and vacuum gates')
    monkeypatch.setattr(LocalStore, 'prune_events_step', forbidden)
    monkeypatch.setattr(LocalStore, 'incremental_vacuum_step', forbidden)
    for turn in range(30):
        run_owner_retention_if_due(home, now=200_000 + turn * 61)
        if _aligned(store):
            break
    assert _aligned(store)


def test_low_space_index_does_not_starve_original_retention(tmp_path, monkeypatch):
    import os

    home, store, _ = _legacy(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text())
    policy['cache_days'] = 1
    home.owner_retention_json.write_text(json.dumps(policy))
    cache = home.owner_cache_dir / 'old.tmp'
    cache.write_text('expired')
    os.utime(cache, (1, 1))
    with store._connection() as conn:
        conn.executemany("INSERT INTO events(event_id,event_type,created_at) VALUES(?, 'seed', 1)",
                         ((f'old-{i}',) for i in range(20_010)))
        # 前缀裁剪不能越过较新的头事件；让本夹具整个待裁剪前缀都确实过期。
        conn.execute('UPDATE events SET created_at=1')
        conn.commit()
    proxy = SimpleNamespace(**vars(maintenance.os))
    proxy.statvfs = lambda path: SimpleNamespace(f_bavail=0, f_frsize=4096)
    monkeypatch.setattr(maintenance, 'os', proxy)
    run_owner_retention_if_due(home, now=200_000)
    state = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
    assert state['local_store_fts_upgrade']['status'] == 'deferred'
    assert not cache.exists()
    with store._connection() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM events WHERE event_type='seed'").fetchone()[0]
    assert remaining < 20_010
    assert not _aligned(store)


def test_event_interleaving_keeps_original_job_and_final_exact_content(tmp_path, monkeypatch):
    from threading import Event, Thread

    home, store, items = _legacy(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    ready, written, errors = Event(), Event(), []
    original = maintenance._fts_prepare
    def paused(current):
        snapshot = original(current)
        ready.set()
        assert written.wait(5)
        return snapshot
    def writer():
        try:
            assert ready.wait(5)
            other = LocalStore(store.db_path)
            other.upsert_record(source_type='memory', source_id='0', title='updated', content='interleavedneedle')
            assert other.delete_record(items[1].id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            written.set()
    thread = Thread(target=writer)
    thread.start()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(maintenance, '_fts_prepare', paused)
            run_owner_retention_if_due(home, now=1000)
    finally:
        written.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    with store._connection() as conn:
        job_id = conn.execute('SELECT job_id FROM maintenance_fts_job').fetchone()[0]
    for turn in range(30):
        run_owner_retention_if_due(home, now=1061 + turn * 61)
        if _aligned(store):
            break
    assert _aligned(store)
    with store._connection() as conn:
        assert conn.execute('SELECT job_id FROM maintenance_fts_job').fetchone()[0] == job_id
    assert {r.id for r in store._search_fts('interleavedneedle', limit=10, source_type=None, visibility=None)} == {items[0].id}
    assert not store._search_fts('needle1', limit=10, source_type=None, visibility=None)


def test_unreadable_maintenance_cursor_is_not_overwritten_as_empty(tmp_path):
    home, store, _ = _legacy(tmp_path)
    path = home.owner_data_dir / 'maintenance.json'
    path.write_bytes(b'{broken-state')
    with pytest.raises(ValueError, match='FTS_MAINTENANCE_SOURCE_UNREADABLE') as caught:
        run_owner_retention_if_due(home, now=1000)
    assert caught.value.load_error
    assert path.read_bytes() == b'{broken-state'
    assert not _aligned(store)


def test_workspace_discovery_uses_one_persistent_page_not_full_listing(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_py_agent.agent.local_storage.fts_upgrade import advance_upgrade, next_upgrade_at

    home = ensure_my_agent_home(tmp_path)
    workspaces = home.owner_home_dir / 'workspace' / 'runtime' / 'workspaces'
    workspaces.mkdir(parents=True)
    for number in range(1000):
        (workspaces / f'scope-{number}').mkdir()
    def forbidden(*args, **kwargs):
        pytest.fail('projection discovery must not enumerate all workspaces')
    monkeypatch.setattr(Path, 'iterdir', forbidden)
    assert next_upgrade_at(home.owner_home_dir, {}, 1000) == 0
    first = advance_upgrade(home.owner_home_dir, {}, 1000)
    second = advance_upgrade(home.owner_home_dir, {'local_store_fts_upgrade': first}, 1061)
    assert first['status'] == second['status'] == 'discovery'
    assert first['cursor']['directory']['last_scanned'] == 1
    assert second['cursor']['directory']['last_scanned'] == 1
    assert first['cursor']['directory'] != second['cursor']['directory']


def test_portable_workspace_page_preserves_small_scale_upgrade(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import directory_paging

    home, store, _ = _legacy(tmp_path)
    destination = home.owner_home_dir / 'workspace' / 'runtime' / 'workspaces' / 'scope' / 'local_store'
    destination.parent.mkdir(parents=True)
    store.db_path.parent.rename(destination)
    store = LocalStore(destination / 'local.db')
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    monkeypatch.setattr(directory_paging, '_native_directory_supported', lambda: False)
    try:
        for turn in range(30):
            run_owner_retention_if_due(home, now=1000 + turn * 61)
            if _aligned(store):
                break
        assert _aligned(store)
    finally:
        # 便携枚举器是原32流有界缓存；只释放本夹具owner持有的流，不回收其它测试资源。
        with directory_paging._SCAN_LOCK:
            for key in tuple(directory_paging._SCAN_STREAMS):
                if key[1] == str(home.owner_home_dir):
                    directory_paging._SCAN_STREAMS.pop(key)[0].close()


def test_unicode61_active_survives_until_formal_target_publish(tmp_path):
    home, store, items = _legacy(tmp_path)
    with store._connection() as conn:
        conn.execute('DROP TABLE records_fts')
        conn.execute('CREATE VIRTUAL TABLE records_fts USING fts5(id UNINDEXED,title,content)')
        conn.executemany('INSERT INTO records_fts(id,title,content) VALUES(?,?,?)',
                         ((item.id, item.title, item.content) for item in items))
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('records_fts_schema_version','1')")
        conn.commit()
    store = LocalStore(store.db_path)
    with store._connection() as conn:
        assert conn.execute('SELECT count(*) FROM records_fts').fetchone()[0] == len(items)
        assert conn.execute("SELECT value FROM metadata WHERE key='records_fts_schema_version'").fetchone()[0] == '1'
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    for turn in range(40):
        run_owner_retention_if_due(home, now=1000 + turn * 61)
        assert {r.id for r in store._search_fts('legacy', limit=10, source_type=None, visibility=None)} == {r.id for r in items}
        if _aligned(store):
            break
    assert _aligned(store)
    with store._connection() as conn:
        assert 'trigram' in conn.execute("SELECT sql FROM sqlite_master WHERE name='records_fts'").fetchone()[0]
        assert conn.execute("SELECT value FROM metadata WHERE key='records_fts_schema_version'").fetchone()[0] == '2'


@pytest.mark.parametrize('first_deferred', [False, True])
def test_owner_store_rotation_preserves_other_job_progress(tmp_path, monkeypatch, first_deferred):
    home, second, _ = _legacy(tmp_path)
    first = LocalStore(home.owner_data_dir / 'local_store.sqlite3')
    for number in range(600 if not first_deferred else 1):
        first.upsert_record(source_type='memory', source_id=str(number), title='first', content='firstneedle')
    with first._connection() as conn:
        conn.execute('DELETE FROM metadata WHERE key=?', (FTS_ROWID_ALIGNED_KEY,))
        conn.commit()
    original = maintenance._fts_check_space
    def check_space(current):
        if first_deferred and current.db_path == first.db_path:
            raise maintenance._FtsRebuildDeferred('insufficient space for shadow FTS rebuild')
        return original(current)
    monkeypatch.setattr(maintenance, '_fts_check_space', check_space)
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    for turn in range(60):
        run_owner_retention_if_due(home, now=1000 + turn * 61)
        if _aligned(second):
            break
    assert _aligned(second), 'the first store must not pin all subsequent owner maintenance'
    assert not _aligned(first)
    assert first._search_fts('firstneedle', limit=1, source_type=None, visibility=None)


def test_busy_projection_slice_does_not_wait_for_business_sqlite_timeout(tmp_path):
    import sqlite3
    from threading import Event, Thread

    home, store, _ = _legacy(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    holder = sqlite3.connect(store.db_path)
    holder.execute('BEGIN IMMEDIATE')
    finished, errors = Event(), []
    def maintain():
        try:
            run_owner_retention_if_due(home, now=1000)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
    worker = Thread(target=maintain)
    worker.start()
    try:
        # 持锁者尚未释放时维护必须已返回；不是等释放后再把旧30秒等待说成有界。
        assert finished.wait(3)
    finally:
        holder.rollback()
        holder.close()
        worker.join(5)
    assert not worker.is_alive() and not errors
    state = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
    assert state['local_store_fts_upgrade']['status'] == 'unknown'
    assert not _aligned(store)


def test_projection_slice_does_not_modify_cached_business_connection_timeout(tmp_path):
    from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
    from agent_py_agent.agent.local_storage.fts_upgrade import advance_upgrade

    home, store, _ = _legacy(tmp_path)
    with sqlite_connection_scope(True):
        with store._connection() as conn:
            assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 30_000
        advance_upgrade(home.owner_home_dir, {}, 1000)
        with store._connection() as conn:
            assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 30_000
