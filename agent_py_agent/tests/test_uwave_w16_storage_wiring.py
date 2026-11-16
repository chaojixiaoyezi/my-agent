import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.user_space import owner_maintenance as maintenance
from agent_py_agent.agent.user_space import storage_lanes
from agent_py_agent.agent.user_space.home_layout import home_paths


def setup_wiring(tmp_path, monkeypatch):
    home = replace(home_paths(tmp_path / 'home'), owner_id='local/main')
    home.owner_home_dir.mkdir(parents=True)
    monkeypatch.setattr(maintenance, '_owner_pressure', lambda home: None)
    monkeypatch.setattr(maintenance, '_deferred_quota_reconciliation', lambda *a, **k: {'complete': True})
    config = SimpleNamespace(retention_scan_slice_entries=3, retention_scan_slice_bytes=99, retention_scan_slice_ms=8)
    retention = SimpleNamespace(load_errors=[{'error_code': 'MEMORY_RETENTION_POLICY_UNREADABLE'}],
                                applied=False, legal_hold=False, scan_partial=True)
    return home, config, retention


def test_memory_error_does_not_block_non_destructive_lanes(tmp_path, monkeypatch):
    home, config, retention = setup_wiring(tmp_path, monkeypatch)
    requests = []
    commits = []

    def lane(request):
        requests.append(request)
        assert request.config is config
        assert request.budget.max_items <= 3
        assert request.budget.max_bytes <= 99
        assert 0 < request.budget.deadline - time.monotonic() <= .008
        request.run_write(lambda: commits.append(request.kind))
        return dict(cursor={}, complete=True, items=1, bytes=2)

    monkeypatch.setattr(storage_lanes, 'resolve_storage_lane', lambda kind, **kwargs: lane)
    state = maintenance.run_owner_storage_maintenance_slice(
        home, {}, retention, now=1, coordinator=True, config=config,
        commit_transition=lambda op: op())
    assert {request.kind for request in requests} == {'global_indexes', 'thread_index', 'task_call_telemetry'}
    assert set(commits) == {'global_indexes', 'thread_index', 'task_call_telemetry'}
    assert state['deletion_enabled'] is False
    assert state['deferred_reason'] == 'coordinator_adapter_protocol_required'


def test_outer_exhausted_zero_is_not_reopened(tmp_path, monkeypatch):
    home, config, retention = setup_wiring(tmp_path, monkeypatch)
    monkeypatch.setattr(storage_lanes, 'resolve_storage_lane', lambda kind, **kwargs: pytest.fail('zero budget must not resolve lane'))
    original = maintenance.run_storage_maintenance_slice

    def exhausted(previous, adapters, **kwargs):
        for step in adapters.values():
            receipt = step({}, {'items': 0, 'bytes': 0, 'deadline': time.monotonic() + 10})
            assert receipt['items'] == receipt['bytes'] == 0
            assert receipt['complete'] is False
        return original(previous, {}, **kwargs)

    monkeypatch.setattr(maintenance, 'run_storage_maintenance_slice', exhausted)
    maintenance.run_owner_storage_maintenance_slice(home, {}, retention, now=1,
        coordinator=True, config=config, commit_transition=lambda op: op())


def test_canonical_owner_required_without_guessing(tmp_path, monkeypatch):
    home, config, retention = setup_wiring(tmp_path, monkeypatch)
    home = replace(home, owner_id='')
    monkeypatch.setattr(storage_lanes, 'resolve_storage_lane', lambda kind, **kwargs: pytest.fail('invalid scope must not resolve lane'))
    state = maintenance.run_owner_storage_maintenance_slice(home, {}, retention, now=1,
        coordinator=True, config=config, commit_transition=lambda op: op())
    assert state['errors'][0]['error_type'] == 'ValueError'
    assert state['errors'][0]['side_effects_unknown'] is True


def test_scan_partial_keeps_deletion_protected():
    retention = SimpleNamespace(applied=True, legal_hold=False, load_errors=[], scan_partial=True)
    assert maintenance._retention_deletion_safe(retention) is False


def test_continuation_transports_same_config(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import owner_quota
    home, config, _ = setup_wiring(tmp_path, monkeypatch)
    path = home.owner_home_dir / 'data/maintenance.json'
    path.parent.mkdir()
    path.write_text('{}')
    observed = []

    def storage(*args, **kwargs):
        observed.append(kwargs)
        return dict(partial=False, continuation_pending=False)

    monkeypatch.setattr(maintenance, 'run_owner_storage_maintenance_slice', storage)
    monkeypatch.setattr(owner_quota, 'refresh_owner_quota_host_rows', lambda *args: None)
    previous = dict(maintenance_partial=True, apply_outcome='refused',
                    storage_maintenance={'pending_kinds': ['thread_index']})
    maintenance._run_quota_continuation_slice(path, home, previous, 1, config=config,
                                             include_global_indexes=True)
    assert observed[0]['config'] is config
    assert observed[0]['include_global_indexes'] is True
