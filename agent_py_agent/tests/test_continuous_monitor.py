from __future__ import annotations

from agent_py_agent.agent.ingestion.continuous_monitor import (
    ContinuousProofPolicy,
    discover_owner_homes,
    evaluate_continuous_proof,
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
    report = evaluate_continuous_proof(
        [{"observed_at": 0, "watches": watches}, {"observed_at": 120, "watches": watches}],
        ContinuousProofPolicy(minimum_seconds=120, maximum_sample_gap_seconds=120),
    )
    assert report["proven"] is False
    assert report["reason"] == "insufficient_healthy_guarantee_sources"


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
