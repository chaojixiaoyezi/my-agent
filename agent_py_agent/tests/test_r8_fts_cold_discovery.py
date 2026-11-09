"""冷发现只借首片预算；真实旧job、原生续点和普通维护写门保持。"""
import json
import os
import shutil
import time
from collections import OrderedDict
from threading import Lock
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.local_storage import LocalStore, maintenance
from agent_py_agent.agent.local_storage import fts_upgrade as upgrade
from agent_py_agent.agent.user_space.directory_paging import directory_page
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due


def _current_workspaces(tmp_path, count):
    home = ensure_my_agent_home(tmp_path / 'host')
    template = LocalStore(tmp_path / 'template' / 'local.db')
    template.rebuild_fts()
    for _ in range(20):
        if not template.cleanup_fts_retired_step(max_rows=128, max_bytes=1024*1024, max_ms=1000)['pending']:
            break
    for i in range(count):
        path = home.owner_home_dir / 'workspace/runtime/workspaces' / f'scope-{i}' / 'local_store/local.db'
        path.parent.mkdir(parents=True)
        shutil.copyfile(template.db_path, path)
        path.chmod(0o600)
    return home


def _names(home):
    names, cursor = [], {}
    while True:
        page, cursor, complete = directory_page(home.owner_home_dir, upgrade._WORKSPACES, cursor, 64, require_dir_fd=False)
        names.extend(page)
        if complete:
            return names


def _restart(monkeypatch):
    monkeypatch.setattr(upgrade, '_HOT_HINTS', (os.getpid(), Lock(), OrderedDict()))


def test_cold_restart_finds_same_old_map_job_among_two_hundred_current_stores(tmp_path, monkeypatch):
    home = _current_workspaces(tmp_path, 200)
    target = _names(home)[159]
    store = LocalStore(home.owner_home_dir / 'workspace/runtime/workspaces' / target / 'local_store/local.db')
    items = [store.upsert_record(source_type='memory', source_id=str(i), title=str(i), content='coldrecoverneedle') for i in range(8)]
    with store._connection() as conn:
        conn.execute('UPDATE records SET rowid=rowid+100')
        conn.execute("DELETE FROM metadata WHERE key IN ('records_fts_rowid_aligned','records_fts_rebuild_pending')")
        conn.commit()
    for step in range(100):
        store.rebuild_fts_step(max_rows=1, max_bytes=1024*1024, max_ms=1000, start_new=step == 0)
        with store._connection() as conn:
            job = maintenance._fts_job(conn)
        if job['phase'] == 'map':
            break
    assert job['phase'] == 'map' and job['copied'] == len(items)
    identity = job['identity']
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    clock = SimpleNamespace(now=time.monotonic())
    proxy = SimpleNamespace(**vars(time))
    proxy.monotonic = lambda: clock.now
    monkeypatch.setattr(upgrade, 'time', proxy)
    found = False
    for turn in range(20):
        _restart(monkeypatch)  # 提示丢失/重启，只有原维护游标与SQLite任务仍在。
        clock.now = time.monotonic()
        run_owner_retention_if_due(home, now=1000+61*turn)
        saved = json.loads((home.owner_data_dir / 'maintenance.json').read_text())['local_store_fts_upgrade']
        if saved.get('store') == store.db_path.relative_to(home.owner_home_dir).as_posix():
            found = True
            break
    assert found, '160个候选应在最多20个原维护机会被发现，而非等160分钟'
    monkeypatch.setattr(upgrade, 'time', time)
    for extra in range(8):
        _restart(monkeypatch)
        run_owner_retention_if_due(home, now=1000+61*(turn+extra+1))
        with store._connection() as conn:
            resumed = maintenance._fts_job(conn)
        if resumed['generation'] > job['generation']:
            break
    assert resumed['generation'] > job['generation']
    assert resumed['identity'] == identity
    assert resumed['copied'] >= len(items)
    assert resumed['phase'] in {'map', 'publish', 'complete'}
    # 全文沿原索引仍可查；本测试没有业务读取制造热提示来驱动发现。
    assert {row.id for row in store._search_fts('coldrecoverneedle', limit=20, source_type=None, visibility=None)} == {row.id for row in items}


def test_current_only_discovery_is_eight_pages_and_stops_at_complete_directory(tmp_path, monkeypatch):
    home = _current_workspaces(tmp_path, 20)
    _restart(monkeypatch)
    proxy = SimpleNamespace(**vars(time))
    proxy.monotonic = lambda: 1000.0
    monkeypatch.setattr(upgrade, 'time', proxy)
    calls = []
    from agent_py_agent.agent.io import sqlite_scope
    connections = []
    connect = sqlite_scope.connect_projection_sqlite

    def opened(*args, **kwargs):
        connections.append(args[0])
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite_scope, 'connect_projection_sqlite', opened)
    original = upgrade.directory_page

    def page(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(upgrade, 'directory_page', page)
    previous = {}
    sizes = []
    for turn in range(3):
        before = len(calls)
        with upgrade.fts_upgrade_tick_scope(deadline=1002, max_steps=50):
            report = upgrade.advance_upgrade(home.owner_home_dir, previous, 1000+61*turn)
        sizes.append(len(calls)-before)
        previous = {'local_store_fts_upgrade': report}
    assert sizes == [8, 8, 5]  # 最后一页是原生EOF，不回绕扫描第一库。
    assert report['discovery_complete']
    assert len(connections) == 20


def test_slow_first_cold_page_does_not_receive_another_slice(tmp_path, monkeypatch):
    home = _current_workspaces(tmp_path, 20)
    _restart(monkeypatch)
    clock = SimpleNamespace(now=1000.0)
    proxy = SimpleNamespace(**vars(time))
    proxy.monotonic = lambda: clock.now
    monkeypatch.setattr(upgrade, 'time', proxy)
    original = upgrade.directory_page
    calls = []

    def slow(*args, **kwargs):
        calls.append(1)
        result = original(*args, **kwargs)
        clock.now += .030
        return result

    monkeypatch.setattr(upgrade, 'directory_page', slow)
    result = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert result['status'] == 'budget_exhausted' and calls == [1]
    assert len(result['cursor']['pending']) == 1
    assert result['catchup']['steps'] == 1


def test_multiple_owners_share_global_steps_and_cold_deadline(tmp_path, monkeypatch):
    first = _current_workspaces(tmp_path / 'first', 20)
    second = _current_workspaces(tmp_path / 'second', 20)
    _restart(monkeypatch)
    proxy = SimpleNamespace(**vars(time))
    proxy.monotonic = lambda: 1000.0
    monkeypatch.setattr(upgrade, 'time', proxy)
    deadlines = []
    original = upgrade._advance_upgrade_slice

    def observe(*args, **kwargs):
        deadlines.append(kwargs['deadline'])
        return original(*args, **kwargs)

    monkeypatch.setattr(upgrade, '_advance_upgrade_slice', observe)
    with upgrade.fts_upgrade_tick_scope(deadline=1002, max_steps=10) as budget:
        a = upgrade.advance_upgrade(first.owner_home_dir, {}, 1000)
        b = upgrade.advance_upgrade(second.owner_home_dir, {}, 1000)
        assert upgrade.advance_upgrade(first.owner_home_dir, {}, 1000) is None
    assert a['catchup']['cold_pages'] == 8 and b['catchup']['cold_pages'] == 2
    assert budget.remaining_steps == 0
    assert len(deadlines) == 10 and set(deadlines) == {1000.025}


def test_unknown_database_or_no_cursor_progress_yields_without_more_cold_io(tmp_path, monkeypatch):
    home = _current_workspaces(tmp_path, 20)
    _restart(monkeypatch)
    first = _names(home)[0]
    path = home.owner_home_dir / 'workspace/runtime/workspaces' / first / 'local_store/local.db'
    path.write_bytes(b'not a sqlite database')
    report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert report['status'] == 'unknown' and report['catchup']['steps'] == 1
    monkeypatch.setattr(upgrade, 'directory_page', lambda *args, **kwargs: ([], {}, False))
    previous = {'local_store_fts_upgrade': {'cursor': {'fixed': 3}, 'next_at': 0}}
    report = upgrade.advance_upgrade(home.owner_home_dir, previous, 1061)
    assert report['status'] == 'discovery' and report['catchup']['steps'] == 1
    assert not report['discovery_advanced']


def test_empty_workspace_discovery_uses_eight_pages_without_sqlite(tmp_path, monkeypatch):
    from agent_py_agent.agent.io import sqlite_scope

    home = ensure_my_agent_home(tmp_path)
    workspaces = home.owner_home_dir.joinpath(*upgrade._WORKSPACES)
    for i in range(200):
        (workspaces / f'expired-{i}').mkdir(parents=True)
    _restart(monkeypatch)
    proxy = SimpleNamespace(**vars(time))
    proxy.monotonic = lambda: 1000.0
    monkeypatch.setattr(upgrade, 'time', proxy)

    def forbidden(*args, **kwargs):
        raise AssertionError('empty workspace must not open SQLite')

    monkeypatch.setattr(sqlite_scope, 'connect_projection_sqlite', forbidden)
    result = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert result['status'] == 'discovery'
    assert result['catchup']['cold_pages'] == 8
    assert result['catchup']['steps'] == 8


@pytest.mark.parametrize('selection', ['hot', 'pending'])
def test_current_hot_or_pending_slot_does_not_borrow_cold_pages(tmp_path, monkeypatch, selection):
    home = _current_workspaces(tmp_path, 20)
    _restart(monkeypatch)
    name = _names(home)[0]
    relative = f'workspace/runtime/workspaces/{name}/local_store/local.db'
    previous = {}
    if selection == 'hot':
        upgrade.note_active_store(home.owner_home_dir / relative)
    else:
        previous = {'local_store_fts_upgrade': {'cursor': {'pending': [relative], 'discover_next': False}}}

    def forbidden(*args, **kwargs):
        pytest.fail('hot/pending opportunity must not start a cold discovery burst')

    monkeypatch.setattr(upgrade, 'directory_page', forbidden)
    report = upgrade.advance_upgrade(home.owner_home_dir, previous, 1000)
    assert report['selection'] == selection and report['status'] == 'current'
    assert report['catchup']['steps'] == 1 and report['catchup']['cold_pages'] == 0
