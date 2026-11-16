"""D-3: a partial owner maintenance run that repeats the previous run's progress facts waits for the pressure re-due
interval; a partial that advances, or an old marker without the new keys, still continues on the next tick."""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.gateway_parts.multiprocess_background_sources import maintenance_due
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionReport
from agent_py_agent.agent.user_space import owner_maintenance as maintenance
from agent_py_agent.agent.user_space import owner_quota
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

T0 = 200_000.0
REDUE = maintenance._OWNER_MAINTENANCE_PRESSURE_REDUE_SECONDS


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text())
    policy["maintenance_interval_seconds"] = 86_400
    home.owner_retention_json.write_text(json.dumps(policy))
    monkeypatch.setattr(maintenance, "_owner_pressure_for_root", lambda _root: None)
    monkeypatch.setattr(owner_quota, "owner_quota_reconcile_pending", lambda *_a, **_k: False)
    return home


@pytest.fixture
def script(monkeypatch):
    """Each full run takes the next (retention scan cursor, storage lane cursor) pair; both stay partial."""
    steps: list[tuple[dict, dict]] = []
    current: dict = {}

    def retention(home, *, now=None, config=None, scan_budget=None):
        current["step"] = steps.pop(0)
        return MemoryRetentionReport(applied=True, actions=(), scan_partial=True, scan_cursor=current["step"][0])

    def storage(home, previous, retention, **_kwargs):
        return {"schema_generation": 1, "partial": True, "continuation_pending": True, "pending_kinds": ["thread_index"],
                "cursors": {"thread_index": {**current["step"][1], "updated_at": _kwargs["now"]}},
                "completed": [], "items": 0, "bytes": 0}

    monkeypatch.setattr(maintenance, "apply_owner_retention", retention)
    monkeypatch.setattr(maintenance, "run_owner_storage_maintenance_slice", storage)
    return steps


def _run(home, now):
    result = maintenance.run_owner_retention_if_due(home, now=now, include_global_indexes=False)
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text())
    return result, marker


def _due(home, now):
    return maintenance.owner_maintenance_due(home.owner_home_dir, now=now)


def test_repeated_progress_facts_wait_for_the_redue_interval(home, script):
    script.extend([({"pins": {"steps": 128}}, {"offset": 10})] * 2)
    first, marker = _run(home, T0)
    assert first.ran and marker["maintenance_partial"] is True and marker["maintenance_partial_stalled"] is False
    assert _due(home, T0 + 1)
    second, marker = _run(home, T0 + 1)
    assert second.ran and marker["maintenance_partial_stalled"] is True
    assert not _due(home, T0 + 2)
    assert _run(home, T0 + 2)[0].ran is False
    assert not _due(home, T0 + REDUE)
    assert _due(home, T0 + 1 + REDUE)


@pytest.mark.parametrize("advance", ["scan_cursor", "lane_cursor"])
def test_advancing_partial_keeps_continuing_every_tick(home, script, advance):
    script.append(({"pins": {"steps": 128}}, {"offset": 10}))
    script.append(({"pins": {"steps": 256}}, {"offset": 10}) if advance == "scan_cursor"
                  else ({"pins": {"steps": 128}}, {"offset": 20}))
    _run(home, T0)
    _result, marker = _run(home, T0 + 1)
    assert marker["maintenance_partial_stalled"] is False
    assert _due(home, T0 + 2)


def test_stalled_partial_resumes_ticking_once_it_advances(home, script):
    script.extend([({"pins": {"steps": 128}}, {"offset": 10})] * 2 + [({"pins": {"steps": 256}}, {"offset": 10})])
    _run(home, T0)
    _run(home, T0 + 1)
    _result, marker = _run(home, T0 + 1 + REDUE)
    assert marker["maintenance_partial_stalled"] is False
    assert _due(home, T0 + 2 + REDUE)


def test_old_partial_marker_without_progress_keys_stays_due(home, script):
    script.append(({"pins": {"steps": 128}}, {"offset": 10}))
    _run(home, T0)
    path = home.owner_data_dir / "maintenance.json"
    marker = json.loads(path.read_text())
    for key in ("maintenance_partial_progress", "maintenance_partial_stalled"):
        marker.pop(key)
    path.write_text(json.dumps(marker))
    assert _due(home, T0 + 1)


def test_multiprocess_discovery_shares_the_same_pacing(home, script):
    from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy

    script.extend([({"pins": {"steps": 128}}, {"offset": 10})] * 2)
    _run(home, T0)
    _result, state = _run(home, T0 + 1)
    policy = MemoryRetentionPolicy.from_payload(json.loads(home.owner_retention_json.read_text()))
    assert maintenance_due(state, policy, T0 + 2, quota_pending=False) is False
    assert maintenance_due(state, policy, T0 + 2, quota_pending=True) is True
    assert maintenance_due(state, policy, T0 + 1 + REDUE, quota_pending=False) is True
    assert maintenance_due({**state, "maintenance_partial_stalled": False}, policy, T0 + 2, quota_pending=False) is True
