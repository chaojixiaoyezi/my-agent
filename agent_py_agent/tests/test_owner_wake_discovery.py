"""磁盘级 owner 唤醒发现(§1 睡死叫不醒):扫描判定、身份构造、登记表种入。"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
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


def _write_task(owner_home: Path, day: str, name: str, status: str) -> None:
    work_dir = owner_home / "tasks" / day / name / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "state.json").write_text(
        json.dumps({"task_id": name, "status": status}),
        encoding="utf-8",
    )


def test_owner_with_running_solo_task_is_discovered(tmp_path) -> None:
    """H 批:普通任务账本 RUNNING(非子代理投影)也是待驱动硬事实——否则重启/逐出后
    owner 永不进池,任务停摆(真机 celery 复刻 RUNNING 6h 无人驱动)。"""
    owners = tmp_path / "owners"
    home = _owner_home(owners, "unknown", "users", "u-celery")
    _write_task(home, "2026-08-08", "继续把-celery-用-go-语言复刻完", "RUNNING")

    page = discover_wake_pending_owner_page(owners, limit=16)

    assert [o.owner_id for o in page.owners] == ["u-celery"]
    assert [o.owner_id for o in page.hard_owners] == ["u-celery"]
    assert page.soft_owners == ()


def test_owner_with_only_terminal_tasks_not_discovered(tmp_path) -> None:
    owners = tmp_path / "owners"
    home = _owner_home(owners, "unknown", "users", "u-tdone")
    for status in ["DONE", "CANCELLED", "FAILED", "ABANDONED", "PAUSED"]:
        _write_task(home, "2026-08-08", f"task-{status}", status)

    assert discover_wake_pending_owners(owners) == []


def test_task_ledger_bad_json_skipped(tmp_path) -> None:
    owners = tmp_path / "owners"
    home = _owner_home(owners, "unknown", "users", "u-bad")
    bad = home / "tasks" / "2026-08-08" / "broken" / "work"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "state.json").write_text("{not json", encoding="utf-8")

    assert discover_wake_pending_owners(owners) == []


def test_task_ledger_planning_pending_blocked_all_count(tmp_path) -> None:
    owners = tmp_path / "owners"
    home = _owner_home(owners, "unknown", "users", "u-multi")
    for status in ["PLANNING", "PENDING", "BLOCKED"]:
        _write_task(home, "2026-08-08", f"task-{status}", status)
    _write_task(home, "2026-08-07", "old-day", "RUNNING")  # 跨天也认

    found = discover_wake_pending_owners(owners)

    assert [(o.provider, o.owner_id) for o in found] == [("unknown", "u-multi")]


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


class _FrozenUTC:
    """固定 UTC 时刻的 datetime 替身:测 daily「白天不挂 / 23 点后挂」不依赖真实跑测时刻。"""

    fromisoformat = staticmethod(datetime.fromisoformat)  # 发现层解析 state 时间戳也用 datetime

    def __init__(self, iso_ts: str) -> None:
        self._dt = datetime.fromisoformat(iso_ts)

    def now(self, tz=None) -> datetime:  # noqa: ARG002 - 测试替身,忽略时区参数
        return self._dt


def test_curator_daily_finalize_missing_does_not_hang_before_hour(monkeypatch, tmp_path) -> None:
    """旧实现「last_daily_finalize_date != 今天 → 每天必挂」;真到期语义:白天不该挂。"""
    from agent_py_agent.agent import owner_wake_discovery

    owners = tmp_path / "owners"
    home = owners / "providers" / "feishu" / "users" / "u-daily"
    _write_curator_state(
        home,
        last_daily_finalize_date=(datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat(),
    )
    monkeypatch.setattr(owner_wake_discovery, "datetime", _FrozenUTC("2026-08-08T14:00:00+00:00"))

    assert discover_wake_pending_owners(owners) == []


def test_curator_daily_finalize_due_after_hour(monkeypatch, tmp_path) -> None:
    from agent_py_agent.agent import owner_wake_discovery

    owners = tmp_path / "owners"
    home = owners / "providers" / "feishu" / "users" / "u-daily"
    _write_curator_state(
        home,
        last_daily_finalize_date=(datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat(),
    )
    monkeypatch.setattr(owner_wake_discovery, "datetime", _FrozenUTC("2026-08-08T23:30:00+00:00"))

    found = discover_wake_pending_owners(owners)
    assert [o.owner_id for o in found] == ["u-daily"]


def test_corrupt_curator_state_not_discovered(tmp_path) -> None:
    """坏 state 不能证明有活:发现层不据此占工位(CuratorService 运行时才报结构化错误)。"""
    owners = tmp_path / "owners"
    home = owners / "providers" / "feishu" / "users" / "u-bad-state"
    _write_curator_state(home, raw="{not-json")

    assert discover_wake_pending_owners(owners) == []


def test_curator_pending_review_candidate_discovered(monkeypatch, tmp_path) -> None:
    """candidates.jsonl 有 user_explicit/tool_verified 待晋升候选 → 策展活,与晋升兜底同源。"""
    from agent_py_agent.agent import owner_wake_discovery

    monkeypatch.setattr(owner_wake_discovery, "datetime", _FrozenUTC("2026-08-08T14:00:00+00:00"))
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

    found = discover_wake_pending_owners(owners)
    assert [o.owner_id for o in found] == ["u-cand"]


def test_candidate_review_status_ignored(monkeypatch, tmp_path) -> None:
    """非 pending_review(如 user_rejected)不算策展活;model_inferred 来源不算(发现层只认结构化)。"""
    from agent_py_agent.agent import owner_wake_discovery

    monkeypatch.setattr(owner_wake_discovery, "datetime", _FrozenUTC("2026-08-08T14:00:00+00:00"))
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
