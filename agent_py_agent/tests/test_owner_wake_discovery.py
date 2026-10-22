"""磁盘级 owner 唤醒发现(§1 睡死叫不醒):扫描判定、身份构造、登记表种入。"""

from __future__ import annotations

import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.owner_scoped_pool import ActiveOwnerRegistry
from agent_py_agent.agent.owner_wake_discovery import (
    discover_owner_home_page,
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


def test_pending_or_leased_memory_curator_restores_scoped_owner_after_restart(
    tmp_path,
) -> None:
    owners = tmp_path / "owners"
    pending = _owner_home(owners, "feishu", "users", "u-curator-pending")
    leased = _owner_home(owners, "feishu", "users", "u-curator-lease")
    for owner_home, pending_reasons, active_lease in (
        (pending, ["session_close"], {}),
        (
            leased,
            [],
            {
                "lease_id": "lease-1",
                "run_id": "run-1",
                "reason": "interval",
                "acquired_at": "2026-08-04T00:00:00+00:00",
                "expires_at": "2026-08-04T00:10:00+00:00",
            },
        ),
    ):
        state_path = owner_home / "memory" / "curator" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": "my-agent.memory-curator-state.v1",
                    "last_processed_message_id": "",
                    "last_processed_audit_event_id": "",
                    "per_thread_cursors": {},
                    "last_run_at": "",
                    "last_success_at": "",
                    "last_failure_at": "",
                    "last_failure_code": "",
                    "active_lease": active_lease,
                    "processed_count": 0,
                    "candidate_count": 0,
                    "daily_event_count": 0,
                    "config_revision": "",
                    "pending_reasons": pending_reasons,
                    "pending_requested_at": "2026-08-04T00:00:00+00:00",
                    "last_daily_finalize_date": "",
                }
            ),
            encoding="utf-8",
        )

    found = {owner.owner_id for owner in discover_wake_pending_owners(owners)}

    assert found == {"u-curator-pending", "u-curator-lease"}


def test_unprocessed_conversation_input_restores_owner_without_curator_state(tmp_path) -> None:
    owners = tmp_path / "owners"
    store = _store_root(owners, "feishu", "users", "u-curator-input", "runtime")
    messages = store / "messages"
    messages.mkdir(parents=True)
    (messages / "thread-1.jsonl").write_text('{"message_id":"msg-1"}\n', encoding="utf-8")

    found = discover_wake_pending_owners(owners)

    assert [owner.owner_id for owner in found] == ["u-curator-input"]


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


def _write_task(
    owner_home: Path, day: str, name: str, status: str, task_id: str | None = None
) -> None:
    """写账本投影(tasks/<date>/<name>/work/state.json)。task_id 默认=目录名,
    真实世界账本 task_id 与 link 一致(bind 同源),需要匹配 link 时显式传。"""
    work_dir = owner_home / "tasks" / day / name / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "state.json").write_text(
        json.dumps({"task_id": task_id or name, "status": status}),
        encoding="utf-8",
    )


def _write_link(
    owner_home: Path, task_id: str, status: str, workspace: str = "ws-1"
) -> None:
    """写 conversation task link(生命周期权威):workspace 下 conversations/tasks/<id>.json。"""
    link_dir = (
        owner_home
        / "workspace"
        / "runtime"
        / "workspaces"
        / workspace
        / "conversations"
        / "tasks"
    )
    link_dir.mkdir(parents=True, exist_ok=True)
    (link_dir / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "status": status}),
        encoding="utf-8",
    )


def test_owner_with_running_solo_task_is_discovered(tmp_path) -> None:
    """H 批:任务 link active(非子代理投影)也是待驱动硬事实——否则重启/逐出后
    owner 永不进池,任务停摆(真机 celery 复刻 RUNNING 6h 无人驱动)。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "unknown", "users", "u-celery")
    _write_link(home, "req-celery-1", "active")
    _write_task(
        home, "2026-08-08", "继续把-celery-用-go-语言复刻完", "RUNNING", task_id="req-celery-1"
    )

    page = discover_wake_pending_owner_page(owners, limit=16)

    assert [o.owner_id for o in page.owners] == ["u-celery"]
    assert [o.owner_id for o in page.hard_owners] == ["u-celery"]
    assert page.soft_owners == ()


def test_owner_with_only_terminal_tasks_not_discovered(tmp_path) -> None:
    owners = tmp_path / "owners"
    home = _owner_home(owners, "unknown", "users", "u-tdone")
    for status in ["completed", "done", "cancelled", "failed", "abandoned", "taken_over"]:
        _write_link(home, f"req-{status}", status)
        _write_task(home, "2026-08-08", f"task-{status}", "RUNNING")  # 账本投影残留 RUNNING

    assert discover_wake_pending_owners(owners) == []


def test_ledger_running_but_link_done_not_discovered(tmp_path) -> None:
    """#54 生命周期权威=link:账本 RUNNING 只是投影残留(子代理 DONE 后账本不投影终态,
    真机 7 月旧任务 932h 无终态),link 已终态就不再需要驱动。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "unknown", "users", "u-stale")
    _write_link(home, "subagent-1780468980-688e0b85", "done")
    _write_task(home, "2026-07-03", "派出的子代理有新进展把你唤醒了", "RUNNING")

    assert discover_wake_pending_owners(owners) == []


def test_shadow_task_without_link_not_discovered(tmp_path) -> None:
    """#56 任务分身:task-path: 指纹账本没有 conversation task link(续跑按 task_path
    寻址写账本,却从不 bind link)——link 缺失 = 生命周期未在册,不算未完成,owner 不再
    永久硬活;真实任务(link+账本齐)照常驱动。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "unknown", "users", "u-shadow")
    _write_link(home, "req-real-1", "active")
    _write_task(
        home, "2026-08-08", "真实任务", "RUNNING", task_id="req-real-1"
    )
    _write_task(home, "2026-08-08", "this-is-a-continuation-turn-for", "RUNNING")

    found = discover_wake_pending_owners(owners)

    assert [(o.provider, o.owner_id) for o in found] == [("unknown", "u-shadow")]


def test_task_ledger_bad_json_skipped(tmp_path) -> None:
    owners = tmp_path / "owners"
    home = _owner_home(owners, "unknown", "users", "u-bad")
    _write_link(home, "req-ok", "active")
    _write_task(home, "2026-08-08", "ok-task", "RUNNING", task_id="req-ok")
    link_dir = (
        home
        / "workspace"
        / "runtime"
        / "workspaces"
        / "ws-1"
        / "conversations"
        / "tasks"
    )
    (link_dir / "req-broken.json").write_text("{not json", encoding="utf-8")

    found = discover_wake_pending_owners(owners)

    assert [(o.provider, o.owner_id) for o in found] == [("unknown", "u-bad")]


def test_unfinished_task_ids_cross_workspace_and_dedupe(tmp_path) -> None:
    from agent_py_agent.agent.owner_wake_discovery import unfinished_task_ids

    owners = tmp_path / "owners"
    home = _owner_home(owners, "unknown", "users", "u-multi")
    _write_link(home, "req-a", "active", workspace="ws-1")
    _write_link(home, "req-b", "active", workspace="ws-2")
    _write_link(home, "req-a", "active", workspace="ws-2")  # 跨 workspace 重复 task_id
    _write_link(home, "req-c", "done", workspace="ws-1")
    _write_link(home, "req-d", "cancelled", workspace="ws-2")
    _write_task(home, "2026-08-08", "a-task", "RUNNING", task_id="req-a")
    _write_task(home, "2026-08-08", "b-task", "RUNNING", task_id="req-b")
    _write_task(home, "2026-08-08", "c-task", "RUNNING", task_id="req-c")  # 账本在跑但 link 终态

    found = unfinished_task_ids(home)

    assert sorted(found) == ["req-a", "req-b"]


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


def _write_curator_state(
    owner_home: Path,
    *,
    pending_reasons: list[str] | None = None,
    active_lease: dict | None = None,
    last_success_at: str = "",
    last_failure_at: str = "",
    last_failure_code: str = "",
    last_daily_finalize_date: str = "",
    raw: str | None = None,
) -> None:
    state_path = owner_home / "memory" / "curator" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        state_path.write_text(raw, encoding="utf-8")
        return
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "my-agent.memory-curator-state.v1",
                "last_processed_message_id": "",
                "last_processed_audit_event_id": "",
                "per_thread_cursors": {},
                "last_run_at": "",
                "last_success_at": last_success_at,
                "last_failure_at": last_failure_at,
                "last_failure_code": last_failure_code,
                "active_lease": active_lease or {},
                "processed_count": 0,
                "candidate_count": 0,
                "daily_event_count": 0,
                "config_revision": "",
                "pending_reasons": pending_reasons or [],
                "pending_requested_at": "",
                "last_daily_finalize_date": last_daily_finalize_date,
            }
        ),
        encoding="utf-8",
    )


# LLM: 仅这四个UTC资格场景使用同一instant，time与实际datetime同源；显式传原timezone参数，不依赖或修改宿主TZ。
# 函数用途: 冻结策展发现时钟，并将原UTC测试场景运输给新增的时间配置入口。
def _freeze_curator_utc(monkeypatch, discovery, iso_ts):
    instant = datetime.fromisoformat(iso_ts)
    monkeypatch.setattr(discovery.time, "time", instant.timestamp)
    pending = discovery._has_pending_memory_curator_work

    # LLM: 只替测试中策展predicate的timezone输入，其余参数与完整原函数保持，不模拟资格结果或吞异常。
    # 函数用途: 明确这四条原测试使用UTC场景，而非真实宿主时区。
    def utc_pending(home, **kwargs):
        return pending(home, **{**kwargs, "timezone_name": "UTC"})
    monkeypatch.setattr(discovery, "_has_pending_memory_curator_work", utc_pending)


def test_curator_daily_finalize_missing_does_not_hang_before_hour(monkeypatch, tmp_path) -> None:
    """旧实现「last_daily_finalize_date != 今天 → 每天必挂」;真到期语义:白天不该挂。"""
    from agent_py_agent.agent import owner_wake_discovery

    # 冻结时刻必须与 state 的「昨天」同源(真实今天派生):写死日期会让
    # 昨天==冻结日,判定因「日期相同」而非「白天」而不挂,测试静默失效。
    today = datetime.now(timezone.utc).date().isoformat()
    owners = tmp_path / "owners"
    home = owners / "providers" / "feishu" / "users" / "u-daily"
    _write_curator_state(
        home,
        last_daily_finalize_date=(datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat(),
    )
    _freeze_curator_utc(monkeypatch, owner_wake_discovery, f"{today}T14:00:00+00:00")

    assert discover_wake_pending_owners(owners) == []


def test_curator_daily_finalize_due_after_hour(monkeypatch, tmp_path) -> None:
    from agent_py_agent.agent import owner_wake_discovery

    today = datetime.now(timezone.utc).date().isoformat()
    owners = tmp_path / "owners"
    home = owners / "providers" / "feishu" / "users" / "u-daily"
    _write_curator_state(
        home,
        last_daily_finalize_date=(datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat(),
    )
    _freeze_curator_utc(monkeypatch, owner_wake_discovery, f"{today}T23:30:00+00:00")

    found = discover_wake_pending_owners(owners)
    assert [o.owner_id for o in found] == ["u-daily"]


def test_corrupt_curator_state_not_discovered(tmp_path) -> None:
    """坏 state 不能证明有活:发现层不据此占工位(CuratorService 运行时才报结构化错误)。"""
    owners = tmp_path / "owners"
    home = owners / "providers" / "feishu" / "users" / "u-bad-state"
    _write_curator_state(home, raw="{not-json")

    assert discover_wake_pending_owners(owners) == []


def test_curator_pending_review_candidate_discovered(monkeypatch, tmp_path) -> None:
    """保留原node义务：候选only不是新可执行工作，不唤醒owner或改写候选/state。"""
    from agent_py_agent.agent import owner_wake_discovery

    _freeze_curator_utc(monkeypatch, owner_wake_discovery, "2026-08-08T14:00:00+00:00")
    owners = tmp_path / "owners"
    home = owners / "providers" / "feishu" / "users" / "u-cand"
    _write_curator_state(home, last_success_at=datetime.now(timezone.utc).isoformat())
    (home / "memory" / "candidates.jsonl").write_text(
        json.dumps(
            {"candidate_id": "c-1", "status": "pending_review", "origin": "user_explicit"}
        )
        + "\n",
        encoding="utf-8",
    )

    state_before = (home / "memory" / "curator" / "state.json").read_bytes()
    candidate_before = (home / "memory" / "candidates.jsonl").read_bytes()
    found = discover_wake_pending_owners(owners)
    assert [o.owner_id for o in found] == []
    assert (home / "memory" / "curator" / "state.json").read_bytes() == state_before
    assert (home / "memory" / "candidates.jsonl").read_bytes() == candidate_before


def test_candidate_review_status_ignored(monkeypatch, tmp_path) -> None:
    """非 pending_review(如 user_rejected)不算策展活;model_inferred 来源不算(发现层只认结构化)。"""
    from agent_py_agent.agent import owner_wake_discovery

    _freeze_curator_utc(monkeypatch, owner_wake_discovery, "2026-08-08T14:00:00+00:00")
    owners = tmp_path / "owners"
    home = owners / "providers" / "feishu" / "users" / "u-cand-skip"
    _write_curator_state(home, last_success_at=datetime.now(timezone.utc).isoformat())
    (home / "memory" / "candidates.jsonl").write_text(
        json.dumps({"candidate_id": "c-1", "status": "user_rejected", "origin": "user_explicit"})
        + "\n"
        + json.dumps({"candidate_id": "c-2", "status": "pending_review", "origin": "model_inferred"})
        + "\n",
        encoding="utf-8",
    )

    assert discover_wake_pending_owners(owners) == []


def test_policy_enabled_but_not_due_not_discovered(tmp_path) -> None:
    """只看 enabled 不看 next_due_at 的旧实现会把未到期 policy 当有活;真到期:未来不挂。"""
    owners = tmp_path / "owners"
    store = _store_root(owners, "feishu", "users", "u-future", "runtime")
    policies = store / "progress_policies"
    policies.mkdir(parents=True, exist_ok=True)
    (policies / "policy-x.json").write_text(
        json.dumps({"enabled": True, "next_due_at": time.time() + 3600}),
        encoding="utf-8",
    )

    assert discover_wake_pending_owners(owners) == []


def test_policy_due_discovered(tmp_path) -> None:
    owners = tmp_path / "owners"
    store = _store_root(owners, "feishu", "users", "u-due", "runtime")
    policies = store / "progress_policies"
    policies.mkdir(parents=True, exist_ok=True)
    (policies / "policy-x.json").write_text(
        json.dumps({"enabled": True, "next_due_at": time.time() - 1}),
        encoding="utf-8",
    )

    found = discover_wake_pending_owners(owners)
    assert [o.owner_id for o in found] == ["u-due"]


# ---------------------------------------------------------------------------
# F 批:父任务聚合收口 —— 准入机制(发现层退避 + 硬/软事实分层 + seed 硬先软后)。
# 真机实锤:215 个 feishu 测试号的 curator 软活(pending_reasons)每轮分页被种回登记表,
# 占满 64 容量池,真活 user-a 的 wake 硬事实按字母序最后被 LRU 逐出 → 唤醒永不消费 → 父任务不收口。
# ---------------------------------------------------------------------------


def test_curator_pending_within_failure_backoff_not_discovered(tmp_path) -> None:
    """失败退避窗口(300s)内,pending 的 curator 软活不判活——不再每轮分页占工位。"""
    owners = tmp_path / "owners"
    _write_curator_state(
        _owner_home(owners, "feishu", "users", "u-backoff"),
        pending_reasons=["turn_threshold"],
        last_failure_at=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
        last_failure_code="CURATOR_MODEL_TIMEOUT",
    )

    assert discover_wake_pending_owners(owners) == []


def test_curator_pending_after_backoff_elapsed_discovered_as_soft(tmp_path) -> None:
    """退避窗口(300s)过后,pending 的 curator 软活恢复判活,但归为 soft(可被硬 owner 逐出)。"""
    owners = tmp_path / "owners"
    _write_curator_state(
        _owner_home(owners, "feishu", "users", "u-backoff"),
        pending_reasons=["turn_threshold"],
        last_failure_at=(datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat(),
        last_failure_code="CURATOR_MODEL_TIMEOUT",
    )

    page = discover_wake_pending_owner_page(owners, limit=16)
    assert [o.owner_id for o in page.owners] == ["u-backoff"]
    assert [o.owner_id for o in page.soft_owners] == ["u-backoff"]
    assert page.hard_owners == ()


def test_curator_pending_without_failure_is_soft(tmp_path) -> None:
    """无失败记录的 pending curator 活 → soft(可被硬 owner 逐出),不再与硬事实平权。"""
    owners = tmp_path / "owners"
    _write_curator_state(_owner_home(owners, "feishu", "users", "u-soft"), pending_reasons=["turn_threshold"])

    page = discover_wake_pending_owner_page(owners, limit=16)
    assert [o.owner_id for o in page.owners] == ["u-soft"]
    assert [o.owner_id for o in page.soft_owners] == ["u-soft"]
    assert page.hard_owners == ()


def test_hard_fact_ignores_curator_failure_backoff(tmp_path) -> None:
    """curator 刚失败不掩盖硬事实:同一 owner 有待消费 wake 信号仍判 hard,必进池。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "feishu", "users", "u-mixed")
    _write_curator_state(
        home,
        pending_reasons=["turn_threshold"],
        last_failure_at=(datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
        last_failure_code="CURATOR_MODEL_TIMEOUT",
    )
    _write_wake_signal(_store_root(owners, "feishu", "users", "u-mixed", "runtime"), "urgent", "ws-1")

    page = discover_wake_pending_owner_page(owners, limit=16)
    assert [o.owner_id for o in page.owners] == ["u-mixed"]
    assert [o.owner_id for o in page.hard_owners] == ["u-mixed"]
    assert page.soft_owners == ()


def test_seed_registry_hard_owners_evict_soft_when_full(tmp_path) -> None:
    """登记表满时,分页种入硬 owner 必成功、软 owner 被拒——软活可以等,硬活不能饿死。"""
    from agent_py_agent.agent.owner_wake_discovery import (
        discover_wake_pending_owner_page,
    )

    owners = tmp_path / "owners"
    # 软(curator pending)owner 字母序在前,硬(policy 到期)owner 在后 → 同一页内先扫到软。
    _write_curator_state(_owner_home(owners, "feishu", "users", "u-a-soft1"), pending_reasons=["turn_threshold"])
    _write_curator_state(_owner_home(owners, "feishu", "users", "u-a-soft2"), pending_reasons=["turn_threshold"])
    for name in ("u-z-hard1", "u-z-hard2"):
        _write_policy(_store_root(owners, "feishu", "users", name, "runtime"), "p", enabled=True)
    registry = ActiveOwnerRegistry(max_owners=2)

    page = discover_wake_pending_owner_page(owners, limit=16)
    # owners = hard + soft(硬先软后);软 owner 虽字母序在前,分页内仍排在硬 owner 之后
    assert [o.owner_id for o in page.owners] == ["u-z-hard1", "u-z-hard2", "u-a-soft1", "u-a-soft2"]
    assert [o.owner_id for o in page.hard_owners] == ["u-z-hard1", "u-z-hard2"]
    assert [o.owner_id for o in page.soft_owners] == ["u-a-soft1", "u-a-soft2"]
    seeded = 0
    for owner in page.hard_owners:
        if registry.record(owner, hard=True):
            seeded += 1
    for owner in page.soft_owners:
        if registry.record(owner, hard=False):
            seeded += 1

    assert seeded == 2  # 只有硬 owner 种入,软 owner 全被拒
    ids = {o.owner_id for o in registry.snapshot()}
    assert ids == {"u-z-hard1", "u-z-hard2"}


def test_seed_registry_preserves_hard_owners_when_soft_evicted(tmp_path) -> None:
    """已入表软 owner 被新硬 owner 逐出(而非反过来);用 seed_registry_page_from_disk 全流程。"""
    owners = tmp_path / "owners"
    _write_curator_state(_owner_home(owners, "feishu", "users", "u-a-soft1"), pending_reasons=["turn_threshold"])
    _write_curator_state(_owner_home(owners, "feishu", "users", "u-a-soft2"), pending_reasons=["turn_threshold"])
    _write_policy(_store_root(owners, "feishu", "users", "u-z-hard1", "runtime"), "p", enabled=True)
    _write_policy(_store_root(owners, "feishu", "users", "u-z-hard2", "runtime"), "p", enabled=True)
    registry = ActiveOwnerRegistry(max_owners=2)

    seed_registry_page_from_disk(registry, owners, limit=16)

    assert {o.owner_id for o in registry.snapshot()} == {"u-z-hard1", "u-z-hard2"}
    assert registry.hard_snapshot() != []
    assert registry.soft_snapshot() == []


# ---------------------------------------------------------------------------
# 分页目录快照:一次枚举 + 整体排序供相邻页复用(owner 数 > 页大小时,网关的唤醒种入/
# orphan 发现会在相邻 tick 连跑相邻页,旧实现每页都重扫全部 owner home)。
# 快照只是查询投影:结构签名或 TTL 一变就重新枚举,owner 事实仍然逐页读盘裁决。
# ---------------------------------------------------------------------------


@pytest.fixture
def snapshots():
    """分页快照是模块级缓存:用例前后都清空,避免跨用例复用影响枚举计数与失效断言。"""
    from agent_py_agent.agent import owner_wake_discovery as module

    module._OWNER_HOME_SNAPSHOTS.clear()
    yield module
    module._OWNER_HOME_SNAPSHOTS.clear()


def _count_enumerations(module, monkeypatch) -> dict[str, int]:
    """统计 _candidate_owner_homes 被完整消费的次数(全量枚举的直接证据)。"""
    counter = {"calls": 0}
    original = module._candidate_owner_homes

    def counting(providers_root):
        counter["calls"] += 1
        yield from original(providers_root)

    monkeypatch.setattr(module, "_candidate_owner_homes", counting)
    return counter


def _paged_targets(
    module,
    owners: Path,
    *,
    limit: int,
    after_cursor=None,
    max_pages: int = 50,
) -> list[list[tuple[str, str, str]]]:
    """按游标链翻到终点,返回每页的 (provider, kind, owner_id) 列表。

    有界循环:游标不前进属于回归,必须显式失败而不是把测试挂死。
    """
    pages: list[list[tuple[str, str, str]]] = []
    cursor = after_cursor
    for _ in range(max_pages):
        page = module.discover_owner_home_page(owners, limit=limit, after_cursor=cursor)
        pages.append(
            [
                (target.identity.provider, target.identity.owner_kind, target.identity.owner_id)
                for target in page.targets
            ]
        )
        cursor = page.next_cursor
        if cursor is None:
            return pages
    raise AssertionError("pagination did not terminate")


def test_paged_sweep_enumerates_all_owner_homes_only_once(snapshots, monkeypatch, tmp_path) -> None:
    """owner 数 > 页大小:整轮 7 页只做一次全量枚举,且每页仍只取自己那段。"""
    owners = tmp_path / "owners"
    expected = {f"u{index:03d}" for index in range(200)}
    for owner_id in expected:
        _write_policy(_store_root(owners, "feishu", "users", owner_id, "runtime"), "p", enabled=True)
    counter = _count_enumerations(snapshots, monkeypatch)

    seen: list[str] = []
    pages = 0
    cursor = None
    while True:
        page = discover_wake_pending_owner_page(owners, limit=32, after_cursor=cursor)
        seen.extend(owner.owner_id for owner in page.owners)
        pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break

    assert pages == 7  # 200 / 32
    assert counter["calls"] == 1
    assert len(seen) == 200 == len(set(seen))
    assert set(seen) == expected

    # 紧接着再扫一轮(快照未过期、目录未变)复用同一份枚举:稳态下每轮只枚举一次。
    assert discover_wake_pending_owner_page(owners, limit=32).owners
    assert counter["calls"] == 1


def test_snapshot_reused_across_pages_keeps_same_page_boundaries(snapshots, tmp_path) -> None:
    """复用快照不改变分页边界:页大小/游标链产生的页切分与逐页枚举一致。"""
    owners = tmp_path / "owners"
    for index in range(5):
        _owner_home(owners, "feishu", "users", f"u{index}")

    pages = _paged_targets(snapshots, owners, limit=2)

    assert [[owner_id for _, _, owner_id in page] for page in pages] == [
        ["u0", "u1"],
        ["u2", "u3"],
        ["u4"],
    ]


def test_snapshot_invalidated_when_owner_added_mid_sweep(snapshots, monkeypatch, tmp_path) -> None:
    """分页途中新增 owner:签名变化 → 重新枚举,新 owner 在游标键之后被正常取到。"""
    owners = tmp_path / "owners"
    for index in range(6):
        _owner_home(owners, "feishu", "users", f"u{index}")
    counter = _count_enumerations(snapshots, monkeypatch)

    first = discover_owner_home_page(owners, limit=2)
    assert [target.identity.owner_id for target in first.targets] == ["u0", "u1"]
    bucket_dir = owners / "providers" / "feishu" / "users"
    signature_before = snapshots._owner_home_signature(owners / "providers")

    _owner_home(owners, "feishu", "users", "u9")  # 键排在游标之后,仍应被本轮发现
    assert snapshots._owner_home_signature(owners / "providers") != signature_before
    assert bucket_dir.is_dir()

    rest = _paged_targets(snapshots, owners, limit=2, after_cursor=first.next_cursor)
    ids = [owner_id for page in rest for _, _, owner_id in page]

    assert ids == ["u2", "u3", "u4", "u5", "u9"]
    assert len(ids) == len(set(ids))
    assert counter["calls"] == 2  # 页 1 一次 + 目录变化后重新枚举一次


def test_snapshot_invalidated_when_owner_removed_mid_sweep(snapshots, monkeypatch, tmp_path) -> None:
    """分页途中删除 owner:签名变化 → 重新枚举,已删 owner 不会作为幽灵项返回。"""
    owners = tmp_path / "owners"
    for index in range(6):
        _owner_home(owners, "feishu", "users", f"u{index}")
    counter = _count_enumerations(snapshots, monkeypatch)

    first = discover_owner_home_page(owners, limit=2)
    assert [target.identity.owner_id for target in first.targets] == ["u0", "u1"]

    shutil.rmtree(owners / "providers" / "feishu" / "users" / "u3")
    rest = _paged_targets(snapshots, owners, limit=2, after_cursor=first.next_cursor)
    ids = [owner_id for page in rest for _, _, owner_id in page]

    assert ids == ["u2", "u4", "u5"]
    assert "u3" not in ids
    assert counter["calls"] == 2


def test_snapshot_ttl_bound_forces_reenumeration(snapshots, monkeypatch, tmp_path) -> None:
    """TTL 上界:即使签名不变也不能无限期复用(负 TTL = 立即过期,逐页都必须重新枚举)。"""
    monkeypatch.setattr(snapshots, "_SNAPSHOT_TTL_SECONDS", -1.0)
    owners = tmp_path / "owners"
    for index in range(6):
        _owner_home(owners, "feishu", "users", f"u{index}")
    counter = _count_enumerations(snapshots, monkeypatch)

    pages = _paged_targets(snapshots, owners, limit=2)

    assert [owner_id for page in pages for _, _, owner_id in page] == [
        "u0",
        "u1",
        "u2",
        "u3",
        "u4",
        "u5",
    ]
    assert counter["calls"] == 3  # 3 页 = 3 次全量枚举:过期即失效


def test_owner_home_snapshot_cache_is_bounded(snapshots, tmp_path) -> None:
    """条目数上界:多套 providers 根也不会让快照缓存无限增长。"""
    for index in range(snapshots._SNAPSHOT_MAX_ENTRY_COUNT + 2):
        root = tmp_path / f"owners-{index}"
        _owner_home(root, "feishu", "users", "u0")
        discover_owner_home_page(root, limit=8)

    assert len(snapshots._OWNER_HOME_SNAPSHOTS) <= snapshots._SNAPSHOT_MAX_ENTRY_COUNT


def test_single_page_matches_full_enumeration_order(snapshots, monkeypatch, tmp_path) -> None:
    """owner 数 ≤ 页大小:结果与「直接全量枚举 + 整体排序」逐字一致(零行为变化)。"""
    owners = tmp_path / "owners"
    _owner_home(owners, "feishu", "groups", "g1")
    _owner_home(owners, "feishu", "users", "u1")
    _owner_home(owners, "qq", "users", "u2")
    counter = _count_enumerations(snapshots, monkeypatch)
    expected = [
        (provider, owner_kind, owner_home.name)
        for provider, owner_kind, owner_home in sorted(
            snapshots._candidate_owner_homes(owners / "providers"),
            key=lambda item: snapshots._owner_cursor(item[0], item[1], item[2].name),
        )
    ]
    assert expected == [
        ("feishu", "group", "g1"),
        ("feishu", "user", "u1"),
        ("qq", "user", "u2"),
    ]

    first = discover_owner_home_page(owners, limit=64)
    again = discover_owner_home_page(owners, limit=64)

    for page in (first, again):
        assert [
            (target.identity.provider, target.identity.owner_kind, target.identity.owner_id)
            for target in page.targets
        ] == expected
        assert page.next_cursor is None
        assert page.scanned == 3
    assert counter["calls"] == 2  # 对照用的一次全量枚举 + 首页冷启动枚举(再次调用走快照)


def test_owner_facts_are_read_live_not_cached(snapshots, tmp_path) -> None:
    """快照只投影目录结构:同一份快照的后续页仍然逐页读盘判事实,不当第二套权威状态。"""
    owners = tmp_path / "owners"
    for index in range(4):
        _owner_home(owners, "feishu", "users", f"u{index}")

    first = discover_wake_pending_owner_page(owners, limit=2)
    assert first.owners == ()

    _write_policy(_store_root(owners, "feishu", "users", "u2", "runtime"), "p", enabled=True)
    second = discover_wake_pending_owner_page(owners, limit=2, after_cursor=first.next_cursor)

    assert [owner.owner_id for owner in second.owners] == ["u2"]


def test_concurrent_pagination_reads_consistent_pages(snapshots, tmp_path) -> None:
    """三个调用方在不同线程翻页(网关实况):共享快照不产生异常或缺失项。"""
    owners = tmp_path / "owners"
    expected = {f"u{index:03d}" for index in range(60)}
    for owner_id in expected:
        _write_policy(_store_root(owners, "feishu", "users", owner_id, "runtime"), "p", enabled=True)

    def sweep() -> set[str]:
        found: set[str] = set()
        cursor = None
        while True:
            page = discover_wake_pending_owner_page(owners, limit=16, after_cursor=cursor)
            found.update(owner.owner_id for owner in page.owners)
            cursor = page.next_cursor
            if cursor is None:
                return found

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: sweep(), range(8)))

    assert results == [expected] * 8


# LLM: 终态 run + 未完成收口事实必须仍被判定为硬事实：否则 Gateway 的 reconcile 车道
# 不会来推进待重试收口，恢复链永不执行（run/task 已终态、也没有 wake 信号）。
# 函数用途: 验证 runtime_closeout_pending 投影能让 owner 重新进入硬事实车道。
def test_owner_with_pending_runtime_closeout_is_a_hard_fact(tmp_path) -> None:
    owners = tmp_path / "owners"
    home = _owner_home(owners, "unknown", "users", "u-closeout")
    # 终态 run（无未完成状态）+ 待重试收口事实。
    run_dir = home / "agents" / "subagent-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(
        json.dumps(
            {
                "schema_version": "owner-agent-projection.v1",
                "agent_id": "subagent-1",
                "run_id": "subagent-1",
                "status": "FAILED",
                "runtime_closeout_pending": True,
            }
        ),
        encoding="utf-8",
    )

    found = discover_wake_pending_owners(owners)

    assert [(o.provider, o.owner_id) for o in found] == [("unknown", "u-closeout")]


# LLM: 反向用例——终态且没有待重试收口事实的 run 不得被当成硬事实，
# 否则每次 reconcile 都会无意义地扫全量 owner（软活不能伪装成硬活）。
# 函数用途: 验证终态 run 不会仅因存在就被判为硬事实。
def test_terminal_run_without_closeout_is_not_a_hard_fact(tmp_path) -> None:
    owners = tmp_path / "owners"
    home = _owner_home(owners, "unknown", "users", "u-quiet")
    _write_run(home, "subagent-1", "FAILED")

    found = discover_wake_pending_owners(owners)

    assert found == []


# LLM: 使用真实磁盘账本，只将终态 mtime 调旧；不模拟状态或发现结果。
# 函数用途: 创建大目录性能测试的历史终态和单个活跃账本。
def _settled_history(home, root_name="runs", count=100):
    day = home / root_name / "2026-10-20"
    old_ns = time.time_ns() - 120_000_000_000
    settled = set()
    for index in range(count + 1):
        path = day / f"run-{index:05d}" / "work" / "state.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"task_id": f"task-{index}", "status": "DONE" if index < count else "RUNNING"}))
        if index < count:
            os.utime(path, ns=(old_ns, old_ns))
            settled.add(path)
    _write_link(home, f"task-{count}", "active")
    return settled, path


# LLM: 在真正的 Path.stat/os.scandir 边界计数；调用者仅在测试体内持有 monkeypatch。
# 函数用途: 记录历史 state 文件 stat 与日期目录重枚举，防止只换扫描 API 而未降本。
def _count_history_io(monkeypatch, settled, day):
    calls = {"settled_stat": 0, "date_scan": 0}
    real_stat, real_scan = Path.stat, os.scandir

    # LLM: 只观察指定旧文件，其余参数、返回值和异常原样传递。
    # 函数用途: 记录每次旧终态文件的元数据读取。
    def stat(path, *args, **kwargs):
        if path in settled:
            calls["settled_stat"] += 1
        return real_stat(path, *args, **kwargs)

    # LLM: 只观察指定日期目录；不替换枚举结果。
    # 函数用途: 记录每次日期目录的子运行名枚举。
    def scandir(path):
        if Path(path) == day:
            calls["date_scan"] += 1
        return real_scan(path)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(os, "scandir", scandir)
    return calls


# LLM: 两个账本布局共享同一合同；终态100条不应在稳态重新stat，RUNNING仍必须即刻检测。
# 函数用途: 验证发现和摘要共用历史缓存、活跃收口、日期枚举复用。
@pytest.mark.parametrize("root_name", ["runs", "tasks"])
def test_settled_history_skips_stats_and_detects_active_settlement(tmp_path, monkeypatch, root_name):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    home = tmp_path / "home"
    settled, active = _settled_history(home, root_name)
    assert discovery.unfinished_task_ids(home) == ["task-100"]
    before = discovery._owner_fact_signature_digest(home)[0]
    with monkeypatch.context() as patch:
        calls = _count_history_io(patch, settled, active.parents[2])
        for _ in range(3):
            assert discovery.unfinished_task_ids(home) == ["task-100"]
            assert discovery._owner_fact_signature_digest(home)[0] == before
        assert calls == {"settled_stat": 0, "date_scan": 0}
        active.write_text(json.dumps({"task_id": "task-100", "status": "DONE"}))
        assert discovery.unfinished_task_ids(home) == []
        assert discovery._owner_fact_signature_digest(home)[0] != before
        assert calls["settled_stat"] == 0


# LLM: 大链接目录混入lock sidecar，原子替换旧终态关联必须在下一次调用生效。
# 函数用途: 验证链接扫描和签名不重复stat旧终态，且目录变化立即失效。
def test_settled_links_reuse_metadata_and_atomic_replacement_invalidates(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    home = tmp_path / "home"
    _, active = _settled_history(home)
    for index in range(100):
        _write_link(home, f"old-{index}", "completed")
    folder = home / "workspace/runtime/workspaces/ws-1/conversations/tasks"
    old_ns = time.time_ns() - 120_000_000_000
    settled = set(folder.glob("old-*.json"))
    for path in settled:
        os.utime(path, ns=(old_ns, old_ns))
        path.with_suffix(".lock").touch()
    assert discovery.unfinished_task_ids(home) == ["task-100"]
    before = discovery._owner_fact_signature_digest(home)[0]
    with monkeypatch.context() as patch:
        calls = _count_history_io(patch, settled, folder)
        assert discovery.unfinished_task_ids(home) == ["task-100"]
        assert discovery._owner_fact_signature_digest(home)[0] == before
        assert calls == {"settled_stat": 0, "date_scan": 0}
    replacement = folder / "replacement.tmp"
    replacement.write_text(json.dumps({"task_id": "task-100", "status": "completed"}))
    replacement.replace(folder / "task-100.json")
    assert discovery.unfinished_task_ids(home) == []
    assert discovery._owner_fact_signature_digest(home)[0] != before
    assert active.exists()


# LLM: 大消息目录使用最近活跃集合；静态旧消息不应每轮stat，原地追加仍改变摘要。
# 函数用途: 验证单层签名缓存的稳态成本和最近消息追加失效。
def test_large_message_signature_reuses_history_and_detects_append(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    folder = tmp_path / "messages"
    folder.mkdir()
    old_ns = time.time_ns() - 120_000_000_000
    old = []
    for index in range(100):
        path = folder / f"thread-{index:03d}.jsonl"
        path.write_text("{}\n")
        os.utime(path, ns=(old_ns + index, old_ns + index))
        old.append(path)
    active = folder / "active.jsonl"
    active.write_text("{}\n")
    before = discovery._dir_entries_signature(folder, "*.jsonl")
    with monkeypatch.context() as patch:
        calls = _count_history_io(patch, set(old[:60]), folder)
        assert discovery._dir_entries_signature(folder, "*.jsonl") == before
        assert calls == {"settled_stat": 0, "date_scan": 0}
        with active.open("a") as stream:
            stream.write('{"new":true}\n')
        assert discovery._dir_entries_signature(folder, "*.jsonl") != before


# LLM: 元数据读取同时拦截Path与DirEntry，避免改换API后重复stat被漏计。
# 函数用途: 验证策展输入每个匹配文件最多读取一次元数据，不读取lock。
def test_curator_input_stats_each_jsonl_once(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    folder = tmp_path / "audit"
    folder.mkdir()
    for name in ("a.jsonl", "b.jsonl", "c.lock"):
        (folder / name).write_text("{}\n")
    calls = []
    path_stat, entry_stat = Path.stat, os.DirEntry.stat

    # LLM: 原样执行stat，只记录被测audit目录下的文件读取。
    # 函数用途: 为两种扫描API提供相同的元数据计数器。
    def stat(path, *args, **kwargs):
        if Path(path.path if isinstance(path, os.DirEntry) else path).parent == folder:
            calls.append(path.name)
        return (entry_stat if isinstance(path, os.DirEntry) else path_stat)(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", stat)
        patch.setattr(os.DirEntry, "stat", stat)
        assert not discovery._curator_input_newer_than(tmp_path, time.time() + 3600)
    assert sorted(calls) == ["a.jsonl", "b.jsonl"]


# LLM: 配置位置不可见只能阻止无关联补关，不能掩盖默认位置已在跑的任务。
# 函数用途: 验证额外存储异常时默认任务发现仍按原合同工作。
def test_missing_extra_link_directory_keeps_default_active_tasks(tmp_path):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    home = tmp_path / "home"
    _settled_history(home, count=1)
    assert discovery.unfinished_task_ids(home, conversation_tasks_dir=tmp_path / "missing") == ["task-1"]


# LLM: 原地修改旧终态不改变日期目录；用monotonic推进兜底复核，不依赖sleep。
# 函数用途: 验证历史缓存到期后从磁盘重读，且新增运行立即被发现。
def test_history_rechecks_external_terminal_edit_and_new_run(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    home = tmp_path / "home"
    settled, active = _settled_history(home)
    assert discovery.unfinished_task_ids(home) == ["task-100"]
    before = discovery._owner_fact_signature_digest(home)[0]
    old = min(settled)
    old.write_text(json.dumps({"task_id": "reopened", "status": "RUNNING"}))
    _write_link(home, "reopened", "active")
    clock = time.monotonic()
    monkeypatch.setattr(discovery.time, "monotonic", lambda: clock + 61)
    assert set(discovery.unfinished_task_ids(home)) == {"task-100", "reopened"}
    assert discovery._owner_fact_signature_digest(home)[0] != before
    new = active.parents[2] / "new-run" / "work" / "state.json"
    new.parent.mkdir(parents=True)
    new.write_text(json.dumps({"task_id": "new", "status": "RUNNING"}))
    _write_link(home, "new", "active")
    assert set(discovery.unfinished_task_ids(home)) == {"task-100", "reopened", "new"}


# LLM: 固定真实DirEntry的顺序，悬空项不能让后续有效输入被跳过。
# 函数用途: 回归独立审查发现的单项消失掩盖同目录输入问题。
def test_curator_dangling_entry_does_not_hide_newer_input(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent import owner_wake_discovery as discovery

    folder = tmp_path / "audit"
    folder.mkdir()
    (folder / "a.jsonl").symlink_to(folder / "missing")
    (folder / "b.jsonl").write_text("{}\n")
    real_scan = os.scandir

    # LLM: 只排序真实目录条目；不伪造文件或stat结果。
    # 函数用途: 稳定复现先遇悬空项、后遇有效输入的顺序。
    @contextmanager
    def ordered_scan(root):
        with real_scan(root) as entries:
            yield iter(sorted(entries, key=lambda entry: entry.name))

    monkeypatch.setattr(os, "scandir", ordered_scan)
    assert discovery._curator_input_newer_than(tmp_path, 0)


# LLM: 将状态LRU预算缩小模拟单日期超过预算；目录名投影仍必须复用。
# 函数用途: 防止大日期目录完整构建后自淘汰，造成每轮冷扫描。
def test_directory_projection_outlives_status_lru_budget(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    home = tmp_path / "home"
    settled, active = _settled_history(home, count=40)
    monkeypatch.setattr(discovery, "_TASK_STATUS_CACHE_MAX_ENTRIES_COUNT", 32)
    assert discovery.unfinished_task_ids(home) == ["task-40"]
    with monkeypatch.context() as patch:
        calls = _count_history_io(patch, settled, active.parents[2])
        assert discovery.unfinished_task_ids(home) == ["task-40"]
        assert calls == {"settled_stat": 0, "date_scan": 0}


# LLM: 确定性模拟一次耗时超过TTL的真实元数据扫描，不模拟文件状态或摘要结果。
# 函数用途: 防止大消息目录扫描结束时TTL已过，导致永远无法命中缓存。
def test_slow_flat_scan_starts_ttl_after_completion(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    folder = tmp_path / "messages"
    folder.mkdir()
    old_ns = time.time_ns() - 120_000_000_000
    for index in range(100):
        path = folder / f"{index}.jsonl"
        path.write_text("{}\n")
        os.utime(path, ns=(old_ns, old_ns))
    clock, reads = [1000.0], []
    original = discovery._directory_record

    # LLM: 真实读取后仅推进受控时钟，每条20ms使冷扫描超过1秒TTL。
    # 函数用途: 记录扫描量并重现生产慢盘上的缓存自过期。
    def slow_read(path, mode):
        result = original(path, mode)
        reads.append(path)
        clock[0] += 0.02
        return result

    monkeypatch.setattr(discovery.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(discovery, "_directory_record", slow_read)
    first = discovery._dir_entries_signature(folder, "*.jsonl")
    reads.clear()
    assert discovery._dir_entries_signature(folder, "*.jsonl") == first
    assert len(reads) == 32


# LLM: 未知账本状态不得被当成已沉淀终态跳过重检；修复为RUNNING后下一轮必须立即发现。
# 函数用途: 保证仅合法终态进入历史复用，未知状态保持每轮实时复核。
def test_unknown_ledger_status_remains_live_until_settled(tmp_path):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    home = tmp_path / "home"
    _settled, active = _settled_history(home, count=40)
    old_ns = time.time_ns() - 120_000_000_000
    active.write_text(json.dumps({"task_id": "task-40", "status": "UNKNOWN_STATE"}))
    os.utime(active, ns=(old_ns, old_ns))
    assert discovery.unfinished_task_ids(home) == []
    active.write_text(json.dumps({"task_id": "task-40", "status": "RUNNING"}))
    assert discovery.unfinished_task_ids(home) == ["task-40"]


# LLM: PAUSED不唤醒但可恢复，旧mtime和未变日期目录不能令它落入60秒历史复核。
# 函数用途: 验证40条历史终态旁的暂停运行在原地恢复后下一次调用立即被发现。
def test_scaling_d_paused_run_resumes_without_date_directory_change(tmp_path):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    home = tmp_path / "home"
    _settled, active = _settled_history(home, count=40)
    active.write_text(json.dumps({"task_id": "task-40", "status": "PAUSED"}))
    old_ns = time.time_ns() - 120_000_000_000
    os.utime(active, ns=(old_ns, old_ns))
    day = active.parents[2]
    before = day.stat().st_mtime_ns
    assert discovery.unfinished_task_ids(home) == []
    active.write_text(json.dumps({"task_id": "task-40", "status": "RUNNING"}))
    assert day.stat().st_mtime_ns == before
    assert discovery.unfinished_task_ids(home) == ["task-40"]
