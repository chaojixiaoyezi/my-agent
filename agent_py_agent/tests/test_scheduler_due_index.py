from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from agent_py_agent.agent.owner_scoped_pool import ActiveOwnerRegistry
from agent_py_agent.agent.scheduler import (
    SchedulerDueIndex,
    SchedulerJobCreateRequest,
    SchedulerRepository,
)


def _owner(owner_id: str) -> dict[str, object]:
    return {"provider": "feishu", "kind": "user", "id": owner_id}


def _store(*, next_run_at: float = 0.0, run: dict[str, object] | None = None) -> dict[str, object]:
    jobs = {}
    if next_run_at:
        jobs["job-1"] = {"status": "active", "next_run_at": next_run_at}
    runs = {"run-1": run} if run is not None else {}
    return {"jobs": jobs, "runs": runs}


def test_due_index_persists_future_owner_and_claims_only_after_due(tmp_path) -> None:
    path = tmp_path / "global_index" / "scheduler_due.sqlite3"
    first = SchedulerDueIndex(path)
    first.sync_owner(_owner("alice"), _store(next_run_at=200), now=100)

    restarted = SchedulerDueIndex(path)
    assert restarted.claim_due_owners(now=199) == []
    claimed = restarted.claim_due_owners(now=200, lease_seconds=10)

    assert [(row.provider, row.owner_kind, row.owner_id, row.next_due_at) for row in claimed] == [
        ("feishu", "user", "alice", 200.0)
    ]
    assert restarted.claim_due_owners(now=205) == []
    assert [row.owner_id for row in restarted.claim_due_owners(now=211)] == ["alice"]


def test_due_index_schema_init_is_safe_under_concurrent_owner_startup(tmp_path) -> None:
    path = tmp_path / "global_index" / "scheduler_due.sqlite3"
    barrier = threading.Barrier(12)
    failures: list[Exception] = []
    lock = threading.Lock()

    def initialize() -> None:
        barrier.wait()
        try:
            SchedulerDueIndex(path)
        except Exception as exc:
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=initialize) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert SchedulerDueIndex(path).snapshot() == []


def test_due_index_is_owner_keyed_and_removes_owner_without_active_facts(tmp_path) -> None:
    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")
    index.sync_owner(_owner("alice"), _store(next_run_at=200), now=100)
    index.sync_owner(_owner("bob"), _store(next_run_at=150), now=100)

    assert [row.owner_id for row in index.snapshot()] == ["bob", "alice"]

    index.sync_owner(_owner("bob"), _store(), now=120)
    assert [row.owner_id for row in index.snapshot()] == ["alice"]


def test_due_index_tracks_unpublished_and_claimed_runs(tmp_path) -> None:
    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")
    index.sync_owner(
        _owner("queued"),
        _store(run={"status": "queued", "dispatch_after": 120}),
        now=100,
    )
    index.sync_owner(
        _owner("running"),
        _store(run={"status": "running", "claim_expires_at": 130}),
        now=100,
    )

    assert [(row.owner_id, row.next_due_at) for row in index.snapshot()] == [
        ("queued", 120.0),
        ("running", 130.0),
    ]


def test_due_index_repairs_pre_index_owner_ledgers_once(tmp_path) -> None:
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "legacy-user"
    scheduler = owner_home / "data" / "scheduler"
    scheduler.mkdir(parents=True)
    (scheduler / "store.json").write_text(
        json.dumps({**_store(next_run_at=200), "updated_at": 100}),
        encoding="utf-8",
    )
    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")

    first = index.repair_legacy_owner_ledgers(tmp_path / "owners")
    second = index.repair_legacy_owner_ledgers(tmp_path / "owners")

    assert first == {"completed": True, "scanned": 1, "indexed": 1, "errors": 0}
    assert second == {"completed": True, "scanned": 0, "indexed": 0, "errors": 0}
    assert [(row.provider, row.owner_kind, row.owner_id) for row in index.snapshot()] == [
        ("feishu", "user", "legacy-user")
    ]


def test_due_index_retries_legacy_scan_after_unreadable_owner_store(tmp_path) -> None:
    owner_home = tmp_path / "owners" / "providers" / "feishu" / "users" / "broken-user"
    scheduler = owner_home / "data" / "scheduler"
    scheduler.mkdir(parents=True)
    store_path = scheduler / "store.json"
    store_path.write_text("[]", encoding="utf-8")
    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")

    first = index.repair_legacy_owner_ledgers(tmp_path / "owners")
    store_path.write_text(
        json.dumps({**_store(next_run_at=200), "updated_at": 100}),
        encoding="utf-8",
    )
    second = index.repair_legacy_owner_ledgers(tmp_path / "owners")

    assert first == {"completed": False, "scanned": 1, "indexed": 0, "errors": 1}
    assert second == {"completed": True, "scanned": 1, "indexed": 1, "errors": 0}
    assert [row.owner_id for row in index.snapshot()] == ["broken-user"]


def test_due_index_legacy_scan_does_not_follow_owner_symlinks(tmp_path) -> None:
    outside = tmp_path / "outside-owner" / "data" / "scheduler"
    outside.mkdir(parents=True)
    (outside / "store.json").write_text(
        json.dumps({**_store(next_run_at=200), "updated_at": 100}),
        encoding="utf-8",
    )
    users = tmp_path / "owners" / "providers" / "feishu" / "users"
    users.mkdir(parents=True)
    (users / "linked-user").symlink_to(outside.parent.parent, target_is_directory=True)
    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")

    report = index.repair_legacy_owner_ledgers(tmp_path / "owners")

    assert report == {"completed": True, "scanned": 0, "indexed": 0, "errors": 0}
    assert index.snapshot() == []


def test_owner_repository_projects_raw_identity_without_exposing_canonical_path(tmp_path) -> None:
    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")
    repository = SchedulerRepository(
        tmp_path / "owners" / "alice" / "data" / "scheduler",
        owner_provider="feishu",
        owner_kind="user",
        owner_id="providers/feishu/users/alice",
        due_owner_id="alice",
        due_index=index,
    )

    repository.create_job(
        SchedulerJobCreateRequest(
            name="backup",
            prompt="check backup",
            thread_id="thread-a",
            schedule={"kind": "at", "at": "1970-01-01T00:03:20Z", "timezone": "UTC"},
            now=100,
        )
    )

    assert [(row.provider, row.owner_kind, row.owner_id) for row in index.snapshot()] == [
        ("feishu", "user", "alice")
    ]


def test_gateway_due_controller_records_only_claimed_scoped_owners(monkeypatch, tmp_path) -> None:
    from agent_py_agent.cli import gateway_loops

    index = SchedulerDueIndex(tmp_path / "scheduler.sqlite3")
    index.sync_owner(_owner("alice"), _store(next_run_at=100), now=50)
    index.sync_owner(
        {"provider": "local", "kind": "main", "id": "local/main"},
        _store(next_run_at=100),
        now=50,
    )
    registry = ActiveOwnerRegistry()
    base = SimpleNamespace(
        config=SimpleNamespace(
            gateway_request_poll_interval=0.2,
            gateway_heartbeat_interval=5,
            owner_agent_pool_max_agents=64,
            background_owner_workers=8,
        ),
        scheduler_repository=SimpleNamespace(due_index=index),
        home_paths=SimpleNamespace(global_index_dir=tmp_path),
    )
    context = SimpleNamespace(agent=SimpleNamespace())
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base)
    monkeypatch.setattr(gateway_loops, "shared_active_owner_registry", lambda _agent: registry)

    controller = gateway_loops._GatewaySchedulerDueController(context)
    assert controller.tick(now=100) == 1
    assert [(row.provider, row.owner_kind, row.owner_id) for row in registry.snapshot()] == [
        ("feishu", "user", "alice")
    ]
