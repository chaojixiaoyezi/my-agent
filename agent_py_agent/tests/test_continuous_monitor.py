from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
    from agent_py_agent.agent.ingestion import continuous_monitor

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
        )
        for watch_id in ("inactive", "unavailable", "active")
    }
    ensured: list[str] = []
    monkeypatch.setattr(continuous_monitor, "list_states", lambda _home: rows)
    monkeypatch.setattr(
        continuous_monitor,
        "load_state",
        lambda _home, watch_id: states.get(watch_id),
    )
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
