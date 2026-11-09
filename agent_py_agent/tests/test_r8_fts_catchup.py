"""有限追赶共享预算、原owner公平调度及片间真实writer进展。"""
from __future__ import annotations

import json
import os
import time
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.local_storage import fts_upgrade as upgrade
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due


def _seed(home, count=4):
    store = LocalStore(home.owner_data_dir / 'local_store' / 'local.db')
    items = [store.upsert_record(source_type='memory', source_id=str(i), title=str(i),
                                 content=f'catchupneedle old body {i}') for i in range(count)]
    with store._connection() as conn:
        conn.execute('UPDATE records SET rowid=rowid+?', (count + 100,))
        conn.execute("DELETE FROM metadata WHERE key IN ('records_fts_rowid_aligned','records_fts_rebuild_pending')")
        conn.commit()
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    return store, items


def _home(tmp_path, count=4):
    home = ensure_my_agent_home(tmp_path)
    store, items = _seed(home, count)
    return home, store, items


def _fake_step(calls, status='partial', progressed=True, advance=None):
    def step(home, previous, now, **kwargs):
        calls.append(kwargs.get('deadline'))
        if advance is not None:
            advance()
        return {'status': status, 'progressed': progressed, 'complete': False,
                'cursor': {'pending': ['data/local_store/local.db']}, 'next_at': now + 60}
    return step


def test_owner_step_cap_and_nested_global_budget_are_not_reset(tmp_path, monkeypatch):
    home, _, _ = _home(tmp_path)
    calls = []
    monkeypatch.setattr(upgrade, '_advance_upgrade_slice', _fake_step(calls))
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20) as budget:
        report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
        assert report['catchup']['steps'] == upgrade.FTS_UPGRADE_CATCHUP_MAX_STEPS_COUNT
        assert budget.remaining_steps == 4096 - 1024
    calls.clear()
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20, max_steps=3) as outer:
        with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20):
            report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
        assert report['catchup']['steps'] == len(calls) == 3
        assert outer.remaining_steps == 0
        assert upgrade.advance_upgrade(home.owner_home_dir, {}, 1000) is None
    assert upgrade.current_tick_budget() is None


@pytest.mark.parametrize('status', ['partial', 'deferred', 'unsupported_document', 'unknown'])
def test_no_progress_and_deferred_stop_the_burst(tmp_path, monkeypatch, status):
    home, _, _ = _home(tmp_path)
    calls = []
    monkeypatch.setattr(upgrade, '_advance_upgrade_slice', _fake_step(calls, status, False))
    report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert report['catchup']['steps'] == len(calls) == 1
    assert report['status'] == status
    assert report['next_at'] == 1060


def test_owner_and_global_deadlines_are_shared_without_restarting_clock(tmp_path, monkeypatch):
    home, _, _ = _home(tmp_path)
    clock, calls = SimpleNamespace(now=1000.0), []
    proxy = SimpleNamespace(**vars(upgrade.time))
    proxy.monotonic = lambda: clock.now
    monkeypatch.setattr(upgrade, 'time', proxy)
    monkeypatch.setattr(upgrade, '_advance_upgrade_slice',
        _fake_step(calls, advance=lambda: setattr(clock, 'now', clock.now + 0.6)))
    with upgrade.fts_upgrade_tick_scope(deadline=1004):
        first = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
        assert first['catchup']['steps'] == 4
        assert calls[0] == pytest.approx(1000.025)
        assert set(calls[1:]) == {1002.0}
        calls.clear()
        second_started = clock.now
        second = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
        assert second['catchup']['steps'] == 3
        assert calls[0] == pytest.approx(second_started + .025)
        assert set(calls[1:]) == {1004.0}
        assert upgrade.advance_upgrade(home.owner_home_dir, {}, 1000) is None


def test_business_writer_progresses_between_real_slices(tmp_path, monkeypatch):
    home, store, items = _home(tmp_path, 400)
    original, calls = upgrade._advance_upgrade_slice, []
    ready, written, errors = Event(), Event(), []
    writer_after = []
    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if not ready.is_set():
            if result['progressed']:
                # 冷首片可能只提交job或耗尽准备预算；在真实复制进展后才交错writer。
                assert result['progressed']
                writer_after.append(len(calls))
                ready.set()
                assert written.wait(3)
            else:
                assert result['status'] in {'partial', 'budget_exhausted', 'discovery'}
        return result
    def write():
        try:
            assert ready.wait(3)
            other = LocalStore(store.db_path)
            other.upsert_record(source_type='memory', source_id='0', title='changed', content='betweenstepsneedle')
            assert other.delete_record(items[1].id)
        except BaseException as error:
            errors.append(error)
        finally:
            written.set()
    monkeypatch.setattr(upgrade, '_advance_upgrade_slice', observed)
    worker = Thread(target=write)
    worker.start()
    try:
        state = {}
        for turn in range(8):
            maintenance_now = 1000 + turn * 61
            report = upgrade.advance_upgrade(home.owner_home_dir, state, maintenance_now)
            state = {'local_store_fts_upgrade': report}
            if ready.is_set():
                break
        assert ready.is_set(), calls
    finally:
        written.set()
        worker.join(5)
    assert not worker.is_alive() and not errors and writer_after
    for turn in range(30):
        with store._connection() as conn:
            value = conn.execute("SELECT value FROM metadata WHERE key='records_fts_rowid_aligned'").fetchone()
        if value is not None and value[0] == 'true':
            break
        report = upgrade.advance_upgrade(home.owner_home_dir, {'local_store_fts_upgrade': report}, maintenance_now + 61 + turn * 61)
    assert {row.id for row in store._search_fts('betweenstepsneedle', limit=5,
        source_type=None, visibility=None)} == {items[0].id}
    assert store.get_record(items[1].id) is None
    assert any(result['progressed'] for result in calls[writer_after[0]:]), calls
    with store._connection() as conn:
        assert {row[0] for row in conn.execute('SELECT id FROM records_fts')} == {
            item.id for item in items if item.id != items[1].id}


def test_exhausted_index_budget_does_not_consume_ttl_permission_or_work(tmp_path):
    homes = [_home(tmp_path / name)[0] for name in ('one', 'two')]
    for home in homes:
        policy = json.loads(home.owner_retention_json.read_text())
        policy.update(maintenance_enabled=True, cache_days=1)
        home.owner_retention_json.write_text(json.dumps(policy))
        cache = home.owner_cache_dir / 'expired.tmp'
        cache.write_text('expired')
        os.utime(cache, (1, 1))
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20, max_steps=1) as budget:
        for home in homes:
            outcome = run_owner_retention_if_due(home, now=200_000)
            assert outcome.ran and outcome.apply_outcome == 'applied'
            if (home.owner_cache_dir / 'expired.tmp').exists():
                assert outcome.retention is not None and outcome.retention.scan_partial
        assert budget.remaining_steps == 0
        # TTL沿原partial/due后继推进；同一已耗尽FTS预算不能吞掉清理权限或要求一次扫完。
        for turn in range(8):
            if all(not (home.owner_cache_dir / 'expired.tmp').exists() for home in homes):
                break
            for home in homes:
                if (home.owner_cache_dir / 'expired.tmp').exists():
                    outcome = run_owner_retention_if_due(home, now=200_001 + turn)
                    assert outcome.ran and outcome.apply_outcome == 'applied'
            assert budget.remaining_steps == 0
    assert all(not (home.owner_cache_dir / 'expired.tmp').exists() for home in homes)
    second = LocalStore(homes[1].owner_data_dir / 'local_store' / 'local.db')
    with second._connection() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_job'").fetchone() is None


def test_controller_retains_unserved_owner_and_future_hint(tmp_path):
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        ensure_owner_home,
        home_paths_with_owner,
    )
    from agent_py_agent.cli.gateway_loops import (
        STARTUP_GRACE_WINDOW_SECONDS,
        _GatewayOwnerMaintenanceController,
    )

    home = ensure_my_agent_home(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    identities = [OwnerIdentity.provider_user('feishu', name) for name in ('a-first', 'b-second')]
    stores = []
    for identity in identities:
        scoped = home_paths_with_owner(home, ensure_owner_home(home.root, identity))
        stores.append(_seed(scoped)[0])
    agent = SimpleNamespace(home_paths=home,
        config=SimpleNamespace(owner_maintenance_scan_interval_seconds=60, owner_agent_pool_max_agents=64))
    controller = _GatewayOwnerMaintenanceController(SimpleNamespace(agent=agent))
    controller._startup_started_at = time.monotonic() - STARTUP_GRACE_WINDOW_SECONDS - 1
    hints = []
    controller._wake_hints = SimpleNamespace(note_maintenance=lambda *args, **kwargs: hints.append((args, kwargs)))
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20, max_steps=1):
        controller.tick(now=1000)
    second_key = (identities[1].provider, identities[1].owner_kind, identities[1].owner_id)
    assert second_key in controller._priority
    assert hints[-1][1]['not_before'] == 1060
    assert controller._fts_hint_not_before == 1060
    with stores[1]._connection() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_job'").fetchone() is None
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20, max_steps=1):
        controller.tick(now=1061)
    with stores[1]._connection() as conn:
        assert conn.execute("SELECT 1 FROM maintenance_fts_job").fetchone()


def test_hint_sampling_failure_preserves_original_business_exception(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import owner_maintenance
    from agent_py_agent.cli import gateway_loops

    home = ensure_my_agent_home(tmp_path)
    agent = SimpleNamespace(home_paths=home,
        config=SimpleNamespace(owner_maintenance_scan_interval_seconds=60, owner_agent_pool_max_agents=64))
    controller = gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace(agent=agent))
    controller._startup_started_at = time.monotonic() - gateway_loops.STARTUP_GRACE_WINDOW_SECONDS - 1
    original = PermissionError('original maintenance source revoked')
    def failure(*args, **kwargs):
        proxy = SimpleNamespace(**vars(upgrade.time))
        def broken_clock():
            raise RuntimeError('hint clock failed')
        proxy.monotonic = broken_clock
        monkeypatch.setattr(upgrade, 'time', proxy)
        raise original
    monkeypatch.setattr(owner_maintenance, 'invoke_owner_maintenance', failure)
    with pytest.raises(PermissionError) as caught:
        controller.tick(now=1000)
    assert caught.value is original


def test_original_controller_deadline_starts_before_base_owner(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import owner_maintenance
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home
    from agent_py_agent.cli import gateway_loops

    home = ensure_my_agent_home(tmp_path)
    for name in ('one', 'two', 'three'):
        ensure_owner_home(home.root, OwnerIdentity.provider_user('feishu', name))
    clock = SimpleNamespace(now=1000.0)
    for module in (upgrade, gateway_loops):
        proxy = SimpleNamespace(**vars(module.time))
        proxy.monotonic = lambda: clock.now
        monkeypatch.setattr(module, 'time', proxy)
    agent = SimpleNamespace(home_paths=home,
        config=SimpleNamespace(owner_maintenance_scan_interval_seconds=60, owner_agent_pool_max_agents=64))
    controller = gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace(agent=agent))
    controller._startup_started_at = 1000 - gateway_loops.STARTUP_GRACE_WINDOW_SECONDS - 1
    calls = []
    def consume(callback, scoped, **kwargs):
        calls.append(upgrade.current_tick_budget().deadline)
        clock.now += 19 if len(calls) == 1 else 2
        return owner_maintenance.OwnerMaintenanceResult(False, 'not_due')
    monkeypatch.setattr(owner_maintenance, 'invoke_owner_maintenance', consume)
    controller.tick(now=1000)
    assert calls == [1020, 1020]
    assert len(controller._priority) >= 2
    assert controller._fts_hint_not_before == 1060


def test_foreign_pid_budget_never_touches_inherited_lock():
    class PoisonLock:
        def __enter__(self):
            raise AssertionError('inherited lock must not be acquired')
        def __exit__(self, *_args):
            raise AssertionError('inherited lock must not be released')
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20) as budget:
        budget.pid = -1
        budget.lock = PoisonLock()
        assert not budget.reserve()
        assert budget.exhausted()
        assert upgrade.current_tick_budget() is None
    assert upgrade.current_tick_budget() is None


def test_closed_copied_budget_cannot_restart_catchup(tmp_path, monkeypatch):
    from contextvars import copy_context

    home, _, _ = _home(tmp_path)
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20):
        captured = copy_context()
    def forbidden(*args, **kwargs):
        pytest.fail('closed copied budget must not enter a new slice')
    monkeypatch.setattr(upgrade, '_advance_upgrade_slice', forbidden)
    assert captured.run(lambda: upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)) is None


def test_current_receipt_also_discharges_manifest_delta_and_map(tmp_path):
    home, store, _ = _home(tmp_path, 130)
    report = None
    for turn in range(20):
        report = upgrade.advance_upgrade(home.owner_home_dir,
            {'local_store_fts_upgrade': report} if report else {}, 1000 + turn * 61)
        if report['status'] == 'current' and report['complete']:
            break
    assert report['status'] == 'current' and report['complete']
    with store._connection() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_retired'").fetchone() is None
        for table in ('maintenance_fts_docs', 'maintenance_fts_delta', 'maintenance_fts_retired_map'):
            assert conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() is None


def _workspace_legacy(home, name):
    path = home.owner_home_dir / 'workspace' / 'runtime' / 'workspaces' / name / 'local_store' / 'local.db'
    store = LocalStore(path)
    item = store.upsert_record(source_type='memory', source_id='hot', title='hot', content='hothintneedle')
    with store._connection() as conn:
        conn.execute('UPDATE records SET rowid=rowid+100')
        conn.execute("DELETE FROM metadata WHERE key IN ('records_fts_rowid_aligned','records_fts_rebuild_pending')")
        conn.commit()
    return store, item


def test_recent_legacy_store_precedes_cold_workspace_discovery(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path)
    cold, _ = _workspace_legacy(home, 'cold-first')
    hot, item = _workspace_legacy(home, 'recent-hot')
    # 原目录页可晚到任意位置；真实候选及SQLite仍由正式入口复核。
    pages = []
    def cold_page(*args, **kwargs):
        pages.append(True)
        return ['cold-first'], {}, False
    monkeypatch.setattr(upgrade, 'directory_page', cold_page)
    LocalStore(hot.db_path)
    with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20, max_steps=1):
        report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert report['store'] == hot.db_path.relative_to(home.owner_home_dir).as_posix()
    assert pages == [] and report['status'] in {'partial', 'complete'}
    assert {row.id for row in hot.search('hothintneedle')} == {item.id}
    # 提示不改变60秒节流，不让新消息把下一拍变成热自旋。
    assert upgrade.advance_upgrade(home.owner_home_dir, {'local_store_fts_upgrade': report}, 1001) is None


def test_hot_hint_turn_keeps_cold_discovery_and_original_shared_budget(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path)
    cold, _ = _workspace_legacy(home, 'cold')
    hot, _ = _workspace_legacy(home, 'hot')
    cold.rebuild_fts()
    hot.rebuild_fts()
    pages = []
    def cold_page(*args, **kwargs):
        pages.append(True)
        return ['cold'], {}, False
    monkeypatch.setattr(upgrade, 'directory_page', cold_page)
    previous = {}
    selected = []
    for turn in range(4):
        upgrade.note_active_store(hot.db_path)
        with upgrade.fts_upgrade_tick_scope(deadline=time.monotonic() + 20, max_steps=1) as budget:
            report = upgrade.advance_upgrade(home.owner_home_dir, previous, 1000 + 61 * turn)
        assert budget.remaining_steps == 0
        selected.append(report['store'])
        previous = {'local_store_fts_upgrade': report}
    def relative(store):
        return store.db_path.relative_to(home.owner_home_dir).as_posix()
    assert selected == [relative(hot), relative(cold), relative(hot), relative(cold)]
    assert len(pages) == 2


def test_hot_hint_cannot_select_other_owner_or_bypass_revoked_commit(tmp_path, monkeypatch):
    first = ensure_my_agent_home(tmp_path / 'first')
    other = ensure_my_agent_home(tmp_path / 'other')
    local, _ = _workspace_legacy(first, 'first')
    foreign, _ = _workspace_legacy(other, 'foreign')
    LocalStore(foreign.db_path)
    LocalStore(local.db_path)
    from agent_py_agent.agent.io import sqlite_scope
    original_connect = sqlite_scope.connect_projection_sqlite
    opened = []
    def connect(path, *, read_only=False, timeout=5):
        opened.append(path)
        return original_connect(path, read_only=read_only, timeout=timeout)
    monkeypatch.setattr(sqlite_scope, 'connect_projection_sqlite', connect)
    original = PermissionError('maintenance source revoked')
    def revoked(operation):
        raise original
    with pytest.raises(PermissionError) as caught:
        upgrade.advance_upgrade(first.owner_home_dir, {}, 1000, commit=revoked)
    assert caught.value is original
    assert opened and all(path == local.db_path for path in opened)
    for store in (local, foreign):
        with store._connection() as conn:
            assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_job'").fetchone() is None


def test_hot_hint_source_symlink_is_not_opened(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path / 'owner')
    store, _ = _workspace_legacy(home, 'hot')
    LocalStore(store.db_path)
    original = store.db_path.with_name('original.db')
    store.db_path.rename(original)
    store.db_path.symlink_to(original)
    def forbidden(*args, **kwargs):
        pytest.fail('revoked hinted path must not open SQLite')
    monkeypatch.setattr(LocalStore, '_init_schema', forbidden)
    report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert report['scope'] == 'discovery_page' and 'store' not in report


def test_hot_hint_capacity_pid_boundary_and_noncanonical_paths(tmp_path, monkeypatch):
    from collections import OrderedDict
    from threading import Lock

    owners = OrderedDict()
    monkeypatch.setattr(upgrade, '_HOT_HINTS', (os.getpid(), Lock(), owners))
    for owner in range(70):
        for workspace in range(10):
            upgrade.note_active_store(tmp_path / f'owner-{owner}' / 'workspace' / 'runtime' /
                                      'workspaces' / str(workspace) / 'local_store' / 'local.db')
    assert len(owners) == upgrade._FTS_HINT_OWNER_MAX_COUNT
    assert all(len(stores) == upgrade._FTS_HINT_STORE_MAX_COUNT for stores in owners.values())
    before = tuple(owners)
    upgrade.note_active_store(tmp_path / 'external' / 'custom.db')
    upgrade.note_active_store('x' * (upgrade._FTS_HINT_PATH_MAX_CHARS + 1))
    assert tuple(owners) == before
    class ParentLock:
        def __enter__(self):
            pytest.fail('child must never enter inherited hint lock')
    monkeypatch.setattr(upgrade, '_HOT_HINTS', (os.getpid() - 1, ParentLock(), owners))
    upgrade.note_active_store(tmp_path / 'child' / 'data' / 'local_store' / 'local.db')
    assert upgrade._HOT_HINTS[0] == os.getpid() and upgrade._HOT_HINTS[2] is not owners
    assert tuple(owners) == before


def test_hint_failure_does_not_change_constructor_or_legacy_write(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path)
    def unavailable(path):
        raise RuntimeError('optional hint cache failed')
    monkeypatch.setattr(upgrade, 'note_active_store', unavailable)
    store, item = _workspace_legacy(home, 'hot')
    LocalStore(store.db_path)
    updated = store.upsert_record(source_type='memory', source_id='hot', title='changed', content='changedneedle')
    assert updated.id == item.id and store.get_record(item.id).content == 'changedneedle'
    assert {row.id for row in store.search('changedneedle')} == {item.id}


def test_legacy_write_refreshes_only_hint_without_extra_sql_queries(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path)
    store, item = _workspace_legacy(home, 'hot')
    seen, sql = [], []
    def note(path):
        seen.append(path)
    monkeypatch.setattr(upgrade, 'note_active_store', note)
    with store._connection() as conn:
        # 先完成SQLite原虚表连接的config懒加载；只计本次替换及提示新增的语句。
        conn.execute('SELECT 1 FROM records_fts LIMIT 0')
        conn.set_trace_callback(sql.append)
        store._replace_fts_row(conn, item.id, 'changed', 'newneedle')
        conn.commit()
        conn.set_trace_callback(None)
    assert seen == [store.db_path]
    assert [text for text in sql if text.startswith(('SELECT', 'PRAGMA'))] == []
    assert {row.id for row in store.search('newneedle')} == {item.id}


@pytest.mark.parametrize('slow_stage', ['precheck', 'constructor'])
def test_consumed_hot_hint_keeps_selected_store_when_slice_expires(tmp_path, monkeypatch, slow_stage):
    from agent_py_agent.agent.local_storage import maintenance

    home = ensure_my_agent_home(tmp_path)
    hot, item = _workspace_legacy(home, 'hot')
    LocalStore(hot.db_path)
    relative = hot.db_path.relative_to(home.owner_home_dir).as_posix()
    clock = SimpleNamespace(now=time.monotonic())
    proxy = SimpleNamespace(**vars(upgrade.time))
    proxy.monotonic = lambda: clock.now
    original_check = maintenance._local_events_alignment_needed
    original_init = LocalStore.__init__

    def check(connection):
        result = original_check(connection)
        if slow_stage == 'precheck':
            clock.now += .03
        return result

    def initialize(current, db_path, *args, **kwargs):
        original_init(current, db_path, *args, **kwargs)
        if slow_stage == 'constructor' and current.db_path == hot.db_path:
            clock.now += .03

    with monkeypatch.context() as patch:
        patch.setattr(upgrade, 'time', proxy)
        patch.setattr(maintenance, '_local_events_alignment_needed', check)
        patch.setattr(LocalStore, '__init__', initialize)
        report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert report['store'] == relative and report['status'] == 'budget_exhausted'
    assert report['cursor']['pending'] == [relative], 'consumed hot hint must retain its canonical candidate'
    assert upgrade._take_active_store(home.owner_home_dir, ()) is None
    with hot._connection() as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='maintenance_fts_job'").fetchone() is None
    for turn in range(3):
        report = upgrade.advance_upgrade(home.owner_home_dir, {'local_store_fts_upgrade': report}, 1061 + turn * 61)
        if report['complete']:
            break
    assert report['complete']
    with hot._connection() as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='records_fts_rowid_aligned'").fetchone()[0] == 'true'
    assert {row.id for row in hot.search('hothintneedle')} == {item.id}
