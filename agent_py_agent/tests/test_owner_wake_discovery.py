"""磁盘级 owner 唤醒发现(§1 睡死叫不醒):扫描判定、身份构造、登记表种入。"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.owner_scoped_pool import ActiveOwnerRegistry
from agent_py_agent.agent.owner_wake_discovery import (
    discover_wake_pending_owner_page,
    discover_wake_pending_owners,
    seed_registry_from_disk,
    seed_registry_page_from_disk,
)


def _store_root(owners: Path, provider: str, bucket: str, owner_id: str, layout: str) -> Path:
    layouts = {
        "runtime": "workspace/runtime/workspaces/slug-x/conversations",
        "plain": "conversations",
        "data": "data/conversations",
    }
    root = owners / "providers" / provider / bucket / owner_id / layouts[layout]
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_policy(store_root: Path, name: str, *, enabled: bool) -> None:
    policies = store_root / "progress_policies"
    policies.mkdir(parents=True, exist_ok=True)
    (policies / f"{name}.json").write_text(json.dumps({"enabled": enabled}), encoding="utf-8")


def _write_wake_signal(store_root: Path, kind: str, name: str) -> None:
    queue = store_root / "wake_queue" / kind
    queue.mkdir(parents=True, exist_ok=True)
    (queue / f"{name}.json").write_text(json.dumps({"status": "pending"}), encoding="utf-8")


def _write_scheduler_store(owner_home: Path, *, next_run_at: float, with_run: bool = False) -> None:
    root = owner_home / "data" / "scheduler"
    root.mkdir(parents=True, exist_ok=True)
    (root / "store.json").write_text(
        json.dumps(
            {
                "schema_version": "scheduler_store.v1",
                "jobs": {
                    "job-1": {"status": "active", "next_run_at": next_run_at},
                },
                "runs": (
                    {"run-1": {"status": "queued"}}
                    if with_run
                    else {}
                ),
            }
        ),
        encoding="utf-8",
    )


def test_discovers_owner_with_enabled_policy_in_runtime_layout(tmp_path) -> None:
    owners = tmp_path / "owners"
    _write_policy(_store_root(owners, "feishu", "users", "u1", "runtime"), "policy-a", enabled=True)

    found = discover_wake_pending_owners(owners)

    assert [(o.provider, o.owner_kind, o.owner_id) for o in found] == [("feishu", "user", "u1")]


def test_disabled_policy_alone_is_not_discovered(tmp_path) -> None:
    owners = tmp_path / "owners"
    _write_policy(_store_root(owners, "feishu", "users", "u1", "runtime"), "policy-a", enabled=False)

    assert discover_wake_pending_owners(owners) == []


def test_pending_wake_signal_discovered_in_alternate_layouts(tmp_path) -> None:
    owners = tmp_path / "owners"
    _write_wake_signal(_store_root(owners, "feishu", "users", "u2", "data"), "normal", "ws-1")
    _write_wake_signal(_store_root(owners, "qq", "users", "u3", "plain"), "urgent", "ws-2")

    found = {(o.provider, o.owner_id) for o in discover_wake_pending_owners(owners)}

    assert found == {("feishu", "u2"), ("qq", "u3")}


def test_group_bucket_yields_group_identity(tmp_path) -> None:
    owners = tmp_path / "owners"
    _write_policy(_store_root(owners, "feishu", "groups", "g1", "runtime"), "policy-g", enabled=True)

    found = discover_wake_pending_owners(owners)

    assert [(o.owner_kind, o.owner_id) for o in found] == [("group", "g1")]


def test_limit_caps_discovered_owners(tmp_path) -> None:
    owners = tmp_path / "owners"
    for index in range(5):
        _write_policy(_store_root(owners, "feishu", "users", f"u{index}", "runtime"), "p", enabled=True)

    assert len(discover_wake_pending_owners(owners, limit=3)) == 3


def test_paged_discovery_reaches_owners_after_the_cache_limit(tmp_path) -> None:
    owners = tmp_path / "owners"
    expected = {f"u{index:03d}" for index in range(70)}
    for owner_id in expected:
        _write_policy(_store_root(owners, "feishu", "users", owner_id, "runtime"), "p", enabled=True)

    seen: set[str] = set()
    cursor = None
    page_count = 0
    while True:
        page = discover_wake_pending_owner_page(owners, limit=16, after_cursor=cursor)
        seen.update(owner.owner_id for owner in page.owners)
        page_count += 1
        cursor = page.next_cursor
        if cursor is None:
            break

    assert page_count == 5
    assert seen == expected


def test_paged_discovery_bounds_scanned_quiet_owners(tmp_path) -> None:
    owners = tmp_path / "owners"
    for index in range(10):
        _owner_home(owners, "feishu", "users", f"u{index:03d}")
    _write_policy(_store_root(owners, "feishu", "users", "z-pending", "runtime"), "p", enabled=True)

    seen: set[str] = set()
    cursor = None
    pages = []
    while True:
        page = discover_wake_pending_owner_page(owners, limit=3, after_cursor=cursor)
        pages.append(page)
        seen.update(owner.owner_id for owner in page.owners)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert pages[0].owners == ()
    assert all(page.scanned <= 3 for page in pages)
    assert sum(page.scanned for page in pages) == 11
    assert seen == {"z-pending"}


def test_paged_registry_seed_rotates_a_bounded_registry(tmp_path) -> None:
    owners = tmp_path / "owners"
    expected = {f"u{index:03d}" for index in range(9)}
    for owner_id in expected:
        _write_policy(_store_root(owners, "feishu", "users", owner_id, "runtime"), "p", enabled=True)
    registry = ActiveOwnerRegistry(max_owners=4)

    seen: set[str] = set()
    cursor = None
    while True:
        page = seed_registry_page_from_disk(registry, owners, limit=4, after_cursor=cursor)
        seen.update(owner.owner_id for owner in registry.snapshot())
        cursor = page.next_cursor
        if cursor is None:
            break

    assert seen == expected
    assert len(registry.snapshot()) == 4


def test_owner_without_facts_or_missing_dirs_ignored(tmp_path) -> None:
    owners = tmp_path / "owners"
    _store_root(owners, "feishu", "users", "u-empty", "runtime")  # 有存储无事实
    (owners / "providers" / "feishu" / "users" / "u-bare").mkdir(parents=True)  # 裸 owner 目录

    assert discover_wake_pending_owners(owners) == []


def test_due_or_queued_scheduler_fact_restores_owner_after_restart(tmp_path) -> None:
    owners = tmp_path / "owners"
    due = _owner_home(owners, "feishu", "users", "u-due")
    queued = _owner_home(owners, "feishu", "users", "u-queued")
    future = _owner_home(owners, "feishu", "users", "u-future")
    _write_scheduler_store(due, next_run_at=1)
    _write_scheduler_store(queued, next_run_at=9e18, with_run=True)
    _write_scheduler_store(future, next_run_at=9e18)

    found = {owner.owner_id for owner in discover_wake_pending_owners(owners)}

    assert found == {"u-due", "u-queued"}


def test_corrupt_policy_json_treated_as_no_fact(tmp_path) -> None:
    owners = tmp_path / "owners"
    store = _store_root(owners, "feishu", "users", "u-bad", "runtime")
    policies = store / "progress_policies"
    policies.mkdir(parents=True, exist_ok=True)
    (policies / "policy-x.json").write_text("{bad-json", encoding="utf-8")

    assert discover_wake_pending_owners(owners) == []


def test_seed_registry_records_discovered_owners(tmp_path) -> None:
    owners = tmp_path / "owners"
    _write_policy(_store_root(owners, "feishu", "users", "u1", "runtime"), "p", enabled=True)
    _write_wake_signal(_store_root(owners, "feishu", "users", "u2", "runtime"), "urgent", "ws")
    registry = ActiveOwnerRegistry()

    seeded = seed_registry_from_disk(registry, owners)

    assert seeded == 2
    assert {o.owner_id for o in registry.snapshot()} == {"u1", "u2"}


def test_seed_registry_survives_missing_owners_dir(tmp_path) -> None:
    registry = ActiveOwnerRegistry()

    assert seed_registry_from_disk(registry, tmp_path / "owners") == 0
    assert registry.snapshot() == []


# ---------------------------------------------------------------------------
# P2 宿主级重启补口径:在册未完成子代理 run / 未盯完 watch 路,也算待唤醒事实。
# 真机实锤:重启后重新派发的子代理卡 PENDING 12 分钟不恢复——PENDING run 不发 wake
# 信号、盯守 policy 又已退休,旧口径(policy/信号二取一)对这种 owner 完全隐形。
# ---------------------------------------------------------------------------


def _owner_home(owners: Path, provider: str, bucket: str, owner_id: str) -> Path:
    home = owners / "providers" / provider / bucket / owner_id
    home.mkdir(parents=True, exist_ok=True)
    return home


def _write_run(owner_home: Path, run_id: str, status: str) -> None:
    run_dir = owner_home / "agents" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "schema_version": "owner-agent-projection.v1",
                "agent_id": run_id,
                "run_id": run_id,
                "status": status,
            }
        ),
        encoding="utf-8",
    )


def test_manager_task_file_without_owner_projection_is_not_a_discovery_contract(tmp_path) -> None:
    owners = tmp_path / "owners"
    home = _owner_home(owners, "feishu", "users", "u-task-only")
    run_dir = home / "agents" / "subagent-1-aaa"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "task.json").write_text(json.dumps({"status": "RUNNING"}), encoding="utf-8")

    assert discover_wake_pending_owners(owners) == []


def test_owner_with_only_pending_run_is_discovered(tmp_path) -> None:
    owners = tmp_path / "owners"
    home = _owner_home(owners, "feishu", "users", "u-pend")
    _write_run(home, "subagent-1-aaa", "DONE")
    _write_run(home, "subagent-2-bbb", "PENDING")

    found = discover_wake_pending_owners(owners)

    assert [(o.provider, o.owner_id) for o in found] == [("feishu", "u-pend")]


def test_owner_with_only_terminal_runs_not_discovered(tmp_path) -> None:
    owners = tmp_path / "owners"
    home = _owner_home(owners, "feishu", "users", "u-done")
    for index, status in enumerate(["DONE", "CANCELLED", "FAILED", "PAUSED"]):
        _write_run(home, f"subagent-{index}-x", status)

    assert discover_wake_pending_owners(owners) == []


def test_owner_with_incomplete_watch_lane_is_discovered(tmp_path) -> None:
    import time

    from agent_py_agent.agent.ingestion.watch_state import new_state, persist_state, state_dir

    owners = tmp_path / "owners"
    home = _owner_home(owners, "feishu", "users", "u-watch")
    lane = new_state(home, "http://127.0.0.1:9/pull", {"watch_window_seconds": 600})
    lane.opened_at = time.time() - 700.0  # 窗口已走完
    lane.totals["spool_candidates"] = 4  # 未判积压(无 read.json = 0 acked)
    persist_state(lane)

    found = discover_wake_pending_owners(owners)

    assert [(o.provider, o.owner_id) for o in found] == [("feishu", "u-watch")]


def test_closed_or_drained_watch_lane_not_discovered(tmp_path) -> None:
    import time

    from agent_py_agent.agent.ingestion.watch_state import new_state, persist_state, state_dir

    owners = tmp_path / "owners"
    home = _owner_home(owners, "feishu", "users", "u-quiet")
    drained = new_state(home, "http://127.0.0.1:8/pull", {"watch_window_seconds": 600})
    drained.opened_at = time.time() - 700.0
    persist_state(drained)  # 零积压 + 窗口走完
    shut = new_state(home, "http://127.0.0.1:7/pull", {"watch_window_seconds": 600})
    shut.closed = True
    shut.totals["spool_candidates"] = 9
    persist_state(shut)
    (state_dir(home) / f"{shut.watch_id}.read.json").write_text(
        json.dumps(
            {
                "read_seq": 9,
                "candidates_consumed": 9,
                "candidates_acked": 9,
                "updated_at": time.time(),
            }
        ),
        encoding="utf-8",
    )  # close 只停采集；已落盘记录全部有 verdict 后才真正安静

    assert discover_wake_pending_owners(owners) == []
