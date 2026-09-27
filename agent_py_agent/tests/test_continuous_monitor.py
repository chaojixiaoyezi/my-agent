from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.ingestion import continuous_monitor
from agent_py_agent.agent.ingestion import harvester as hv
from agent_py_agent.agent.ingestion import watch_state as ws
from agent_py_agent.agent.ingestion.continuous_monitor import (
    ContinuousProofPolicy,
    discover_owner_homes,
    evaluate_continuous_proof,
    recover_active_audit_harvesters,
)


def _watch(index: int, *, error: str = "") -> dict:
    return {
        "watch_id": f"w{index}",
        "source": {
            "scheme": "https" if index < 2 else "file",
            "origin_hash": f"origin-{index}",
        },
        "source_mode": "cursor" if index < 2 else "file",
        "envelope_keys": [f"schema_{index}"],
        "audit_guarantee": True,
        "totals": {"pulls": 10},
        "last_error_code": error,
        "last_source_at": 1000,
    }


def test_proof_requires_real_elapsed_time_not_accelerated_cycle_count() -> None:
    snapshots = [
        {"observed_at": 1000, "watches": [_watch(1), _watch(2), _watch(3)]},
        {"observed_at": 1060, "watches": [_watch(1), _watch(2), _watch(3)]},
    ]
    report = evaluate_continuous_proof(
        snapshots,
        ContinuousProofPolicy(minimum_seconds=3600, maximum_sample_gap_seconds=120),
    )
    assert report["proven"] is False
    assert report["reason"] == "duration_too_short"


def test_proof_requires_continuous_healthy_heterogeneous_guarantee_sources() -> None:
    snapshots = [
        {"observed_at": 1000, "watches": [_watch(1), _watch(2), _watch(3)]},
        {"observed_at": 1060, "watches": [_watch(1), _watch(2), _watch(3)]},
        {"observed_at": 1120, "watches": [_watch(1), _watch(2), _watch(3)]},
    ]
    report = evaluate_continuous_proof(
        snapshots,
        ContinuousProofPolicy(minimum_seconds=120, maximum_sample_gap_seconds=60),
    )
    assert report == {
        "proven": True,
        "reason": "ok",
        "continuous_seconds": 120,
        "healthy_guarantee_sources": 3,
        "heterogeneous_signatures": 3,
    }


def test_proof_rejects_unhealthy_source() -> None:
    watches = [_watch(1), _watch(2), _watch(3, error="NETWORK")]
    watches[2]["last_source_at"] = 0
    report = evaluate_continuous_proof(
        [{"observed_at": 0, "watches": watches}, {"observed_at": 120, "watches": watches}],
        ContinuousProofPolicy(minimum_seconds=120, maximum_sample_gap_seconds=120),
    )
    assert report["proven"] is False
    assert report["reason"] == "insufficient_healthy_guarantee_sources"


def test_proof_tolerates_transient_error_while_source_fact_is_fresh() -> None:
    snapshots = [
        {"observed_at": 1000, "watches": [_watch(1), _watch(2), _watch(3)]},
        {"observed_at": 1060, "watches": [_watch(1), _watch(2, error="NETWORK"), _watch(3)]},
        {"observed_at": 1120, "watches": [_watch(1), _watch(2), _watch(3)]},
    ]
    report = evaluate_continuous_proof(
        snapshots,
        ContinuousProofPolicy(minimum_seconds=120, maximum_sample_gap_seconds=60),
    )
    assert report["proven"] is True


def test_proof_resets_duration_after_intermediate_source_staleness() -> None:
    stale = [_watch(1), _watch(2), _watch(3)]
    stale[2]["last_source_at"] = 1000
    recovered_1 = [_watch(1), _watch(2), _watch(3)]
    recovered_2 = [_watch(1), _watch(2), _watch(3)]
    for row in recovered_1:
        row["last_source_at"] = 1180
    for row in recovered_2:
        row["last_source_at"] = 1240
    snapshots = [
        {"observed_at": 1000, "watches": [_watch(1), _watch(2), _watch(3)]},
        {"observed_at": 1120, "watches": stale},
        {"observed_at": 1180, "watches": recovered_1},
        {"observed_at": 1240, "watches": recovered_2},
    ]
    report = evaluate_continuous_proof(
        snapshots,
        ContinuousProofPolicy(
            minimum_seconds=180,
            maximum_sample_gap_seconds=120,
            maximum_source_staleness_seconds=60,
        ),
    )
    assert report["proven"] is False
    assert report["continuous_seconds"] == 60
    assert report["reason"] == "duration_too_short"


def test_proof_resets_duration_after_sample_gap_and_can_recover() -> None:
    watches = [_watch(1), _watch(2), _watch(3)]
    snapshots = [
        {"observed_at": 1000, "watches": watches},
        {"observed_at": 1060, "watches": watches},
        {"observed_at": 1300, "watches": watches},
        {"observed_at": 1360, "watches": watches},
        {"observed_at": 1420, "watches": watches},
    ]
    report = evaluate_continuous_proof(
        snapshots,
        ContinuousProofPolicy(minimum_seconds=120, maximum_sample_gap_seconds=120),
    )
    assert report["proven"] is True
    assert report["continuous_seconds"] == 120


def test_proof_rejects_source_that_only_succeeded_in_the_past() -> None:
    watches = [_watch(1), _watch(2), _watch(3)]
    for row in watches:
        row["last_source_at"] = 1000
    report = evaluate_continuous_proof(
        [{"observed_at": 1000, "watches": watches}, {"observed_at": 2000, "watches": watches}],
        ContinuousProofPolicy(
            minimum_seconds=1000,
            maximum_sample_gap_seconds=1000,
            maximum_source_staleness_seconds=300,
        ),
    )
    assert report["proven"] is False
    assert report["reason"] == "insufficient_healthy_guarantee_sources"


def test_owner_discovery_only_walks_canonical_owner_levels(tmp_path) -> None:
    owner = tmp_path / "owners" / "providers" / "local" / "users" / "u1"
    watch_state = owner / "watch_state"
    watch_state.mkdir(parents=True)
    (watch_state / "ws-1234567890.json").write_text("{}", encoding="utf-8")
    decoy = owner / "tasks" / "deep" / "watch_state"
    decoy.mkdir(parents=True)
    (decoy / "ws-decoy.json").write_text("{}", encoding="utf-8")
    assert discover_owner_homes(tmp_path) == [owner]


def test_restart_recovery_only_reacquires_active_named_audit_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    owner_home = tmp_path / "owner"
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner_home))
    rows = [
        {"watch_id": "legacy", "audit_guarantee": False, "closed": False},
        {"watch_id": "inactive", "audit_guarantee": True, "closed": False},
        {"watch_id": "unavailable", "audit_guarantee": True, "closed": False},
        {"watch_id": "active", "audit_guarantee": True, "closed": False},
        {"watch_id": "closed", "audit_guarantee": True, "closed": True},
    ]
    states = {
        watch_id: SimpleNamespace(
            watch_id=watch_id,
            audit_root_task_id=f"task-{watch_id}",
            lock=threading.RLock(),
        )
        for watch_id in ("inactive", "unavailable", "active")
    }
    ensured: list[str] = []
    monkeypatch.setattr(continuous_monitor, "list_states", lambda _home: rows)
    monkeypatch.setattr(
        continuous_monitor.watch_state,
        "registry",
        SimpleNamespace(get_or_load=lambda _home, watch_id: states.get(watch_id)),
    )
    monkeypatch.setattr(continuous_monitor, "refresh_scalars_from_disk", lambda _state: None)
    monkeypatch.setattr(
        "agent_py_agent.agent.ingestion.source_worker.audit_parent_reconcile_state",
        lambda _agent, task_id: {
            "task-inactive": (False, "inactive"),
            "task-unavailable": (False, "unavailable"),
            "task-active": (True, "active"),
        }[task_id],
    )
    monkeypatch.setattr(
        continuous_monitor,
        "ensure_harvester",
        lambda state, _fetch, **_kwargs: ensured.append(state.watch_id)
        or {"mode": "local"},
    )

    assert recover_active_audit_harvesters(agent) == 1
    assert ensured == ["active"]


def test_restart_recovery_without_owner_scope_fails_closed() -> None:
    assert recover_active_audit_harvesters(SimpleNamespace()) == 0


# 函数用途: 在 owner 下落一条仍在运行的命名 Audit 数据源状态，换上干净的 registry，并让父任务检查视为活跃。
def _active_audit_watch(owner_home: Path, monkeypatch) -> str:
    state = ws.new_state(owner_home, "http://src.example/pull", {})
    state.audit_guarantee = True
    state.audit_root_task_id = "task-active"
    ws.persist_state(state)
    monkeypatch.setattr(ws, "registry", ws.WatchRegistry())
    monkeypatch.setattr(
        "agent_py_agent.agent.ingestion.source_worker.audit_parent_reconcile_state",
        lambda _agent, _task_id: (True, "active"),
    )
    return state.watch_id


def test_restart_recovery_hands_the_refreshed_registry_object_to_the_harvester(
    tmp_path: Path,
    monkeypatch,
) -> None:
    owner_home = tmp_path / "owner"
    watch_id = _active_audit_watch(owner_home, monkeypatch)
    # registry 里缓存着一份游标落后的旧对象，盘上游标已前进到 100。
    cached = ws.registry.get_or_load(owner_home, watch_id)
    newer = ws.load_state(owner_home, watch_id)
    newer.cursor = 100
    ws.persist_state(newer)
    captured = []
    monkeypatch.setattr(
        continuous_monitor,
        "ensure_harvester",
        lambda state, _fetch, **_kwargs: captured.append(state) or {"mode": "local"},
    )
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner_home))
    assert recover_active_audit_harvesters(agent) == 1
    assert len(captured) == 1 and captured[0] is cached and ws.registry.get(watch_id) is cached
    assert cached.cursor == 100


def test_pull_after_restart_recovery_reuses_the_harvester_holding_the_registry_object(
    tmp_path: Path,
    monkeypatch,
) -> None:
    owner_home = tmp_path / "owner"
    watch_id = _active_audit_watch(owner_home, monkeypatch)
    held = []

    # 只把收割循环换成记录状态对象、等停止信号的假循环；登记、租约和复用都走真实 ensure_harvester。
    def loop(state, _fetch, stop_event, *rest):
        held.append(state)
        stop_event.wait(10)
        hv._clear_lease(state, rest[0])  # rest[0] 是 lease_id，其后是两个回调

    monkeypatch.setattr(hv, "_harvest_loop", loop)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner_home))
    assert recover_active_audit_harvesters(agent) == 1
    started = hv.harvesters.get_live(watch_id)
    assert started is not None
    try:
        deadline = time.monotonic() + 5
        while not held and time.monotonic() < deadline:
            time.sleep(0.01)
        # 与 pull 入口相同：先经 registry 取对象，再 ensure_harvester（watch_tool 的 pull 与 _pull_from_spool）。
        pulled = ws.registry.get_or_load(owner_home, watch_id)
        assert hv.ensure_harvester(pulled, lambda *_args, **_kwargs: None) == {"mode": "local"}
        assert hv.harvesters.get_live(watch_id) is started
        assert len(held) == 1 and held[0] is pulled
    finally:
        hv.stop_harvester(watch_id)
        started.thread.join(timeout=10)
