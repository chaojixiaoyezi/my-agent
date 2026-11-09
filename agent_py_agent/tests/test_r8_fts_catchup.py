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
        assert set(calls) == {1002.0}
        calls.clear()
        second = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
        assert second['catchup']['steps'] == 3
        assert set(calls) == {1004.0}
        assert upgrade.advance_upgrade(home.owner_home_dir, {}, 1000) is None


def test_business_writer_progresses_between_real_slices(tmp_path, monkeypatch):
    home, store, items = _home(tmp_path, 400)
    original, calls = upgrade._advance_upgrade_slice, []
    ready, written, errors = Event(), Event(), []
    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if len(calls) == 1:
            assert result['progressed']
            ready.set()
            assert written.wait(3)
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
        report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    finally:
        written.set()
        worker.join(5)
    assert not worker.is_alive() and not errors and len(calls) > 1
    for turn in range(30):
        with store._connection() as conn:
            value = conn.execute("SELECT value FROM metadata WHERE key='records_fts_rowid_aligned'").fetchone()
        if value is not None and value[0] == 'true':
            break
        report = upgrade.advance_upgrade(home.owner_home_dir, {'local_store_fts_upgrade': report}, 1061 + turn * 61)
    assert {row.id for row in store._search_fts('betweenstepsneedle', limit=5,
        source_type=None, visibility=None)} == {items[0].id}
    assert store.get_record(items[1].id) is None


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
            run_owner_retention_if_due(home, now=200_000)
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
