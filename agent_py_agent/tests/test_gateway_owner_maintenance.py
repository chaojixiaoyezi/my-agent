from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# LLM: 只替换 gateway_loops 的时间代理，不改全局 time 模块或生产保护期；墙钟 now 仍由各维护调用显式传入。
# 函数用途: 给维护测试提供可推进的单调时钟，避免把刚启动误当成已到期。
@pytest.fixture
def maintenance_clock(monkeypatch):
    from agent_py_agent.cli import gateway_loops

    clock = SimpleNamespace(now=1000.0)
    gateway_time = SimpleNamespace(**vars(gateway_loops.time))
    gateway_time.monotonic = lambda: clock.now
    monkeypatch.setattr(gateway_loops, "time", gateway_time)
    return clock


# LLM: 原维护行为断言只在真实保护期之后执行，不 mock 保护判定、retention 或扫描结果。
# 函数用途: 推进测试单调时钟越过启动保护，再用原墙钟跑维护。
def _tick_after_startup_grace(controller, clock):
    from agent_py_agent.cli import gateway_loops

    clock.now += gateway_loops.STARTUP_GRACE_WINDOW_SECONDS + 1
    return controller.tick(now=200_000)


@pytest.fixture
def retention_slice_clock(monkeypatch):
    """分片时间由测试推进；条目、字节预算和真正的目录/状态读取保持原样。"""
    from agent_py_agent.agent.memory_store import retention_scan_budget

    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(retention_scan_budget, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


# LLM: 业务分页用例观察保护期结束后的真实tick，不删除或替换生产启动保护判定。
# 函数用途: 验证后台维护按owner分页运行而不创建scoped agent。
def test_gateway_owner_maintenance_pages_without_creating_scoped_agents(
    tmp_path: Path,
    monkeypatch,
    maintenance_clock,
) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        ensure_owner_home,
    )
    from agent_py_agent.cli import gateway_loops

    base_home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(
        base_home.root,
        OwnerIdentity.provider_user("feishu", "ou-maintenance"),
    )
    old_cache = owner.cache_dir / "old.tmp"
    old_cache.write_text("old", encoding="utf-8")
    os.utime(old_cache, (1, 1))
    policy = json.loads(owner.retention_json.read_text(encoding="utf-8"))
    policy.update({"cache_days": 1, "maintenance_interval_seconds": 100})
    owner.retention_json.write_text(json.dumps(policy), encoding="utf-8")
    base_agent = SimpleNamespace(
        config=SimpleNamespace(
            owner_maintenance_scan_interval_seconds=60,
            owner_agent_pool_max_agents=64,
        ),
        home_paths=base_home,
    )
    monkeypatch.setattr(
        gateway_loops,
        "_gateway_agent_from_context",
        lambda _context: base_agent,
    )

    controller = gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace())
    summary = _tick_after_startup_grace(controller, maintenance_clock)

    assert summary == {"scanned": 1, "ran": 2, "failed": 0, "refused": 0, "isolated": 0}
    assert not old_cache.exists()
    marker = json.loads((owner.data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert marker["status"] == "success"


# LLM: 只推进测试观测到原180秒保护期之后，保留全部汇总和实际执行断言。
# 函数用途: 验证拒绝与局部隔离错误分别进入正确的维护摘要计数。
def test_gateway_owner_maintenance_summary_separates_refused_from_isolated(
    tmp_path: Path, monkeypatch, capsys, maintenance_clock, retention_slice_clock,
) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home
    from agent_py_agent.cli import gateway_loops

    base_home = ensure_my_agent_home(tmp_path)
    refused = ensure_owner_home(base_home.root, OwnerIdentity.provider_user("feishu", "ou-refused"))
    refused.retention_json.write_text("{broken", encoding="utf-8")
    isolated = ensure_owner_home(base_home.root, OwnerIdentity.provider_user("feishu", "ou-isolated"))
    work = isolated.home_dir / "tasks" / "2026-01-01" / "broken-task" / "work"
    work.mkdir(parents=True)
    (work / "state.json").write_text("{broken", encoding="utf-8")
    base_agent = SimpleNamespace(
        config=SimpleNamespace(owner_maintenance_scan_interval_seconds=60, owner_agent_pool_max_agents=64),
        home_paths=base_home,
    )
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)

    controller = gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace())
    summary = _tick_after_startup_grace(controller, maintenance_clock)

    # failed 只算整次被拒与执行期失败；「执行了、只有隔离错误」只进 isolated（3a 补充裁定）。
    assert summary == {"scanned": 2, "ran": 3, "failed": 1, "refused": 1, "isolated": 1}
    printed = [line for line in capsys.readouterr().out.splitlines() if line.startswith("[gateway-owner-maintenance] ")]
    assert json.loads(printed[-1].split(" ", 1)[1]) == summary


def _controller_for(base_home, monkeypatch):
    from agent_py_agent.cli import gateway_loops

    base_agent = SimpleNamespace(
        config=SimpleNamespace(owner_maintenance_scan_interval_seconds=60, owner_agent_pool_max_agents=64),
        home_paths=base_home,
    )
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)
    return gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace())


@pytest.mark.parametrize("pause_first_slice", [False, True], ids=["single-slice", "resume"])
def test_gateway_owner_maintenance_isolated_only_is_not_failed(
    tmp_path: Path, monkeypatch, maintenance_clock, retention_slice_clock, pause_first_slice,
) -> None:
    from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    base_home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(base_home.root, OwnerIdentity.provider_user("feishu", "ou-isolated-only"))
    work = owner.home_dir / "tasks" / "2026-01-01" / "broken-task" / "work"
    work.mkdir(parents=True)
    (work / "state.json").write_text("{broken", encoding="utf-8")

    controller = _controller_for(base_home, monkeypatch)
    marker_path = owner.data_dir / "maintenance.json"
    if pause_first_slice:
        original_budget = RetentionScanBudget.default

        def elapsed_first_budget(config=None):
            budget = original_budget(config)
            # 模拟发出 25ms 分片后被调度器换出；原有的结束判定必须自行生效。
            retention_slice_clock.now = budget.deadline + 0.001
            return budget

        with monkeypatch.context() as first:
            first.setattr(RetentionScanBudget, "default", staticmethod(elapsed_first_budget))
            partial = _tick_after_startup_grace(controller, maintenance_clock)
        assert partial == {"scanned": 1, "ran": 2, "failed": 0, "refused": 0, "isolated": 0}
        assert json.loads(marker_path.read_bytes())["retention_scan_partial"] is True
        assert (work / "state.json").read_text() == "{broken"
        maintenance_clock.now += 60
        summary = controller.tick(now=200_060)
    else:
        summary = _tick_after_startup_grace(controller, maintenance_clock)

    assert summary == {"scanned": 1, "ran": 2, "failed": 0, "refused": 0, "isolated": 1}
    # 持久化的 status 仍按旧口径记 policy_unavailable，摘要不影响它。
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert (marker["status"], marker["apply_outcome"]) == ("policy_unavailable", "applied")
    # 摘要按 owner 计一次；同一坏文件可以被多个规范来源分别报告。
    assert marker["isolated_error_count"] > 0 and marker["failed_action_count"] == 0
    assert (work / "state.json").read_text() == "{broken"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 可以删只读目录里的文件，造不出执行期失败")
def test_gateway_owner_maintenance_counts_execution_failures_as_failed(tmp_path: Path, monkeypatch, maintenance_clock) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    base_home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(base_home.root, OwnerIdentity.provider_user("feishu", "ou-exec-failure"))
    old_cache = owner.cache_dir / "old.tmp"
    old_cache.write_text("old", encoding="utf-8")
    os.utime(old_cache, (1, 1))
    policy = json.loads(owner.retention_json.read_text(encoding="utf-8"))
    policy.update({"cache_days": 1})
    owner.retention_json.write_text(json.dumps(policy), encoding="utf-8")
    owner.cache_dir.chmod(0o500)
    try:
        summary = _tick_after_startup_grace(_controller_for(base_home, monkeypatch), maintenance_clock)
    finally:
        owner.cache_dir.chmod(0o700)

    assert old_cache.exists()
    assert summary == {"scanned": 1, "ran": 2, "failed": 1, "refused": 0, "isolated": 0}
    marker = json.loads((owner.data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert (marker["apply_outcome"], marker["failed_action_count"], marker["isolated_error_count"]) == ("applied", 1, 0)


def test_gateway_owner_maintenance_waits_for_monotonic_startup_grace(tmp_path: Path, monkeypatch, maintenance_clock) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home
    from agent_py_agent.cli import gateway_loops

    base_home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(base_home.root, OwnerIdentity.provider_user("feishu", "ou-grace-boundary"))
    old_cache = owner.cache_dir / "old.tmp"
    old_cache.write_text("old", encoding="utf-8")
    os.utime(old_cache, (1, 1))
    policy = json.loads(owner.retention_json.read_text(encoding="utf-8"))
    policy.update({"cache_days": 1})
    owner.retention_json.write_text(json.dumps(policy), encoding="utf-8")
    controller = _controller_for(base_home, monkeypatch)
    started = maintenance_clock.now

    for elapsed in (0.0, gateway_loops.STARTUP_GRACE_WINDOW_SECONDS - 0.001):
        maintenance_clock.now = started + elapsed
        assert controller.tick(now=200_000) == {"scanned": 0, "ran": 0, "failed": 0, "refused": 0, "isolated": 0}
        assert old_cache.exists()
        assert not (owner.data_dir / "maintenance.json").exists()

    maintenance_clock.now = started + gateway_loops.STARTUP_GRACE_WINDOW_SECONDS
    assert controller.tick(now=200_000) == {"scanned": 1, "ran": 2, "failed": 0, "refused": 0, "isolated": 0}
    assert not old_cache.exists()
    marker = json.loads((owner.data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert marker["status"] == "success"


# LLM: 只替换已加载repo的清理返回，保留真实分页和cache删除；单owner失败不能中止其它清理及保留。
# 函数用途: 验证安全清理异常按owner隔离并报告warning。
def test_h10_h11_cleanup_failure_isolated_before_retention(tmp_path, monkeypatch, maintenance_clock, caplog):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(home.root, OwnerIdentity.provider_user("feishu", "isolated-cleanup"))
    old = owner.cache_dir / "old.tmp"
    old.write_text("expired")
    os.utime(old, (1, 1))
    policy = json.loads(owner.retention_json.read_text())
    policy["cache_days"] = 1
    owner.retention_json.write_text(json.dumps(policy))
    controller = _controller_for(home, monkeypatch)
    calls = []

    def broken():
        calls.append("broken")
        raise OSError("synthetic cleanup failure")

    good_repo = SimpleNamespace(cleanup_stale_runtime=lambda: calls.append("good"))
    controller._base_agent.subagents = SimpleNamespace(runtime_db=SimpleNamespace(cleanup_stale_runtime=broken))
    controller._base_agent._owner_pool = SimpleNamespace(active_agents=lambda: [
        SimpleNamespace(subagents=SimpleNamespace(runtime_db=good_repo)),
        SimpleNamespace(subagents=SimpleNamespace(runtime_db=good_repo))])
    with caplog.at_level("WARNING"):
        summary = _tick_after_startup_grace(controller, maintenance_clock)
    assert calls == ["broken", "good"]
    assert summary["ran"] == 2 and not old.exists()
    assert any("cleanup" in item.message.lower() for item in caplog.records)


# LLM: 真实runtime库与原cleanup算法，spy只观察原transition是否活跃；安全清理不被retention期限或hold延迟。
# 函数用途: 验证多进程维护沿原写门清理runtime，未到期仍保留原retention不执行口径。
@pytest.mark.parametrize("policy_state", ["due", "not_due", "legal_hold"])
def test_h10_transition_runtime_cleanup_uses_original_commit(tmp_path, monkeypatch, policy_state):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home = ensure_my_agent_home(tmp_path)
    RuntimeRepository(runtime_db_path(home.owner_home_dir))
    if policy_state == "not_due":
        (home.owner_data_dir / "maintenance.json").write_text(json.dumps({"last_attempt_at": 200_000}))
    if policy_state == "legal_hold":
        policy = json.loads(home.owner_retention_json.read_text())
        policy["legal_hold"] = True
        home.owner_retention_json.write_text(json.dumps(policy))
    active, seen = [False], []
    original = RuntimeRepository.cleanup_stale_runtime

    def cleanup(repo, **kwargs):
        result = original(repo, **kwargs)
        seen.append(active[0])
        return result

    def commit(operation):
        assert not active[0]
        active[0] = True
        try:
            return operation()
        finally:
            active[0] = False

    monkeypatch.setattr(RuntimeRepository, "cleanup_stale_runtime", cleanup)
    result = run_owner_retention_if_due(home, now=200_000, include_global_indexes=False, commit_transition=commit)
    assert seen == [True]
    assert result.ran is (policy_state != "not_due")


# LLM: 原状态JSON、DeliveryContext和ReplyEnvelope均真实；只替换管理员发现及外部transport，不发送真实消息。
# 函数用途: 验证按收件人持久去重、部分失败重试、冷却和独立恢复通知，不影响重启预算。
@pytest.mark.parametrize("failure", ["failed", "exception"])
@pytest.mark.parametrize("order", [("admin-A", "admin-B"), ("admin-B", "admin-A")])
def test_h12_partial_admin_alert_retries_only_failed_recipient(tmp_path, monkeypatch, failure, order):
    from agent_py_agent.agent.gateway_parts import supervisor as module
    from agent_py_agent.agent.user_space import admin_channel_identity

    paths = SimpleNamespace(state=tmp_path / "gateway.json", supervisor_state=tmp_path / "supervisor.json")
    paths.state.write_text(json.dumps({"c_run_id": "synthetic-generation"}))
    clock, healthy, calls = [1000.0], [False], []
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(admin_channel_identity, "list_admin_channel_identities", lambda root: [
        SimpleNamespace(channel="tui", user_id=name) for name in order])

    def deliver(context, envelope):
        calls.append((context.target, context.idempotency_key))
        if context.target == "admin-B" and not healthy[0]:
            if failure == "exception":
                raise OSError("synthetic recipient failure")
            return SimpleNamespace(delivery_status="failed")
        return SimpleNamespace(delivery_status="sent")

    agent = SimpleNamespace(config=SimpleNamespace(admin_channel_identity_enabled=True),
        home_paths=SimpleNamespace(root=tmp_path), delivery_service=SimpleNamespace(deliver=deliver))
    supervisor = SimpleNamespace(_paths=paths, _agent=agent, _restart_count=5)
    module._record_supervisor_fault(supervisor, "restart_budget_exhausted")
    assert [target for target, key in calls] == list(order)
    assert json.loads(paths.supervisor_state.read_text())["alert_notified"] is False
    clock[0] += 299
    module._record_supervisor_fault(supervisor, "restart_budget_exhausted")
    assert len(calls) == 2
    healthy[0] = True
    clock[0] += 1
    other = SimpleNamespace(**vars(supervisor))
    module._record_supervisor_fault(other, "restart_budget_exhausted")
    assert [target for target, key in calls] == [*order, "admin-B"]
    assert len({key for target, key in calls[:2]}) == 2
    assert [key for target, key in calls if target == "admin-B"][0] == calls[-1][1]
    assert json.loads(paths.supervisor_state.read_text())["alert_notified"] is True
    clock[0] += 300
    module._record_supervisor_fault(other, "restart_budget_exhausted")
    assert len(calls) == 3
    module._record_supervisor_fault(other, "gateway_recovered", recovered=True)
    assert [target for target, key in calls[-2:]] == list(order)
    assert supervisor._restart_count == other._restart_count == 5


# LLM: 运行时local_store/local.db清理必须携带owner引用保护，不误删仍被候选引用的派生记录。
# 函数用途: 回归验证workspace/runtime/workspaces/*/local_store/local.db过期但受引用记录受保护。
def test_h07_runtime_local_store_respects_owner_protected_refs(tmp_path):
    import sqlite3
    from datetime import datetime, timezone

    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import _prune_local_store_derived_records

    home = ensure_my_agent_home(tmp_path / "home")
    runtime_db = home.owner_home_dir / "workspace" / "runtime" / "workspaces" / "main-1" / "local_store" / "local.db"
    runtime_db.parent.mkdir(parents=True)
    store = LocalStore(runtime_db)
    rec = store.upsert_record(
        source_type="subagent_run",
        source_id="src-protected-1",
        title="Referenced subagent run",
        content="derived body",
        metadata={"status": "done", "conversation_runtime": {"task_id": "active-task"}},
    )
    unreferenced = store.upsert_record(
        source_type="subagent_run",
        source_id="src-expired-2",
        title="Expired subagent run",
        content="expired body",
        metadata={"status": "done"},
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE records SET created_at = ?, updated_at = ?", (1000.0, 1000.0))
    state = home.owner_tasks_dir / "2026-10-26" / "active-task" / "work" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"task_id": "active-task", "status": "RUNNING", "source_refs": [rec.id]}), encoding="utf-8")
    report = _prune_local_store_derived_records(home.owner_home_dir, now=datetime(2026, 10, 26, tzinfo=timezone.utc))
    assert report["executed"] is True
    assert report["deleted_ids"] == [unreferenced.id]
    assert rec.id not in report["deleted_ids"]
    assert store.get_record(rec.id) is not None


# LLM: 原摘要断言逐字保留；另加真实双owner入口，证明新多页控制器不增加摘要键或改变旧计数。
# 函数用途: 验证小规模owner的维护摘要兼容。
def test_w7_two_owner_summary_keys_remain_identical(tmp_path, monkeypatch, maintenance_clock):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    home = ensure_my_agent_home(tmp_path)
    for owner_id in ('w7-first', 'w7-second'):
        ensure_owner_home(home.root, OwnerIdentity.provider_user('feishu', owner_id))
    summary = _tick_after_startup_grace(_controller_for(home, monkeypatch), maintenance_clock)
    assert summary == {'scanned': 2, 'ran': 3, 'failed': 0, 'refused': 0, 'isolated': 0}


def _spawn_cold_sweep_sleeper() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )


def _bind_cold_sweep_task_link(owner_home: Path, *, task_id: str, status: str) -> str:
    from agent_py_agent.agent.conversation.store import ConversationStore

    conv_store = ConversationStore(owner_home / "conversations", initialize=True)
    thread = conv_store.threads.create(title=task_id)
    conv_store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "goal": task_id})
    if status != "active":
        conv_store.tasks.update_status({"task_id": task_id, "status": status, "expected_status": "active"})
    return thread.thread_id


def _insert_cold_sweep_runtime_run(
    owner_home: Path,
    *,
    task_run_id: str,
    task_id: str,
    task_status: str,
    agent_run_id: str,
    run_id: str,
    agent_status: str,
    attempt_id: str,
    attempt_status: str,
    settled_at: float,
) -> None:
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path

    db = runtime_db_path(owner_home)
    RuntimeRepository(db)
    closed_at = settled_at if task_status in {"done", "failed", "cancelled"} else 0.0
    ended_at = settled_at if attempt_status != "running" else 0.0
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO task_runs(task_run_id, task_id, status, created_at, updated_at, closed_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (task_run_id, task_id, task_status, settled_at, settled_at, closed_at),
        )
        conn.execute(
            "INSERT OR REPLACE INTO agent_runs(agent_run_id, task_run_id, parent_agent_run_id, run_id, role, status, "
            "current_attempt_id, current_attempt_generation, workspace_epoch, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_run_id, task_run_id, "", run_id, "main", agent_status, attempt_id, 1, 1, settled_at, settled_at),
        )
        conn.execute(
            "INSERT OR REPLACE INTO agent_attempts(attempt_id, agent_run_id, attempt_generation, status, started_at, ended_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (attempt_id, agent_run_id, 1, attempt_status, settled_at, ended_at),
        )
        conn.commit()


def _write_cold_sweep_process_session(
    owner_home: Path,
    *,
    session_id: str,
    thread_id: str,
    root_task_id: str,
    run_id: str,
    attempt_id: str,
    pid: int,
    started_at: float,
):
    import agent_py_agent.agent.tooling.background_process_launch as bg_launch
    from agent_py_agent.agent.tooling.process_session_records import PROCESS_SESSION_SCHEMA
    from agent_py_agent.agent.tooling.process_session_store import (
        ProcessSessionStore,
        process_session_store_root,
    )

    store = ProcessSessionStore(process_session_store_root(owner_home, owner_home))
    birth_token = bg_launch.capture_process_birth_token(pid) or f"birth-{session_id}"
    store.write(
        {
            "schema": PROCESS_SESSION_SCHEMA,
            "session_id": session_id,
            "command": "sleep 60",
            "cwd": str(owner_home),
            "output_file": str(owner_home / f"{session_id}.log"),
            "host_state_file": "",
            "completion_notice_id": "",
            "launcher_pid": pid,
            "launcher_birth_token": birth_token,
            "pid": pid,
            "pid_birth_token": birth_token,
            "child_pid": pid,
            "child_pid_birth_token": birth_token,
            "child_launch_started": True,
            "handoff_confirmed": True,
            "status": "running",
            "stop_requested": False,
            "retain_until_consumed": True,
            "revision": 0,
            "reserved_at": started_at,
            "started_at": started_at,
            "finished_at": None,
            "exit_code": None,
            "termination": {},
            "completion_target": {},
            "activation_scope": None,
            "access_scope": {
                "owner_id": owner_home.name,
                "conversation_id": thread_id,
                "owner_home": str(owner_home),
            },
            "execution_scope": {
                "owner_home": str(owner_home),
                "thread_id": thread_id,
                "root_task_id": root_task_id,
                "run_id": run_id,
                "attempt_id": attempt_id,
            },
        }
    )
    return store


def test_fw41r2_cold_start_settled_sweep_discovers_and_reclaims_ordinary_local_users(tmp_path: Path) -> None:
    """FW41R2-P1-02: cold-start sweep discovers canonical owners/local/* users and preserves active/unknown."""
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.tooling.process_session_records import PROCESS_TERMINAL_STATUSES
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home
    from agent_py_agent.cli import gateway_process

    base_home = ensure_my_agent_home(tmp_path / "home")
    main_home = ensure_owner_home(base_home.root, OwnerIdentity.local_main())
    alice_home = ensure_owner_home(base_home.root, OwnerIdentity.local_user("alice"))
    bob_home = ensure_owner_home(base_home.root, OwnerIdentity.local_user("bob"))
    provider_home = ensure_owner_home(base_home.root, OwnerIdentity.provider_user("feishu", "ou-provider-1"))

    outside_dir = (tmp_path / "outside_owner").resolve()
    outside_dir.mkdir(parents=True)
    os.symlink(
        outside_dir,
        base_home.root / "owners" / "local" / "linked_owner",
        target_is_directory=True,
    )
    (base_home.root / "owners" / "local" / "non canonical dir").mkdir(parents=True)
    (base_home.root / "owners" / "local" / "stray_file").write_text("x", encoding="utf-8")
    (base_home.root / "owners" / "providers" / "feishu" / "groups" / ".my-agent-runtime").mkdir(
        parents=True, exist_ok=True,
    )

    now = time.time()
    settled_at = now - 3600.0
    procs: list[subprocess.Popen[str]] = []
    try:
        alice_proc = _spawn_cold_sweep_sleeper()
        procs.append(alice_proc)
        alice_thread = _bind_cold_sweep_task_link(alice_home.home_dir, task_id="task-alice", status="completed")
        _insert_cold_sweep_runtime_run(
            alice_home.home_dir,
            task_run_id="tr-alice",
            task_id="task-alice",
            task_status="done",
            agent_run_id="ar-alice",
            run_id="run-alice",
            agent_status="done",
            attempt_id="att-alice",
            attempt_status="done",
            settled_at=settled_at,
        )
        alice_store = _write_cold_sweep_process_session(
            alice_home.home_dir,
            session_id="bg-alice-settled",
            thread_id=alice_thread,
            root_task_id="task-alice",
            run_id="run-alice",
            attempt_id="att-alice",
            pid=alice_proc.pid,
            started_at=settled_at,
        )

        bob_active_proc = _spawn_cold_sweep_sleeper()
        procs.append(bob_active_proc)
        bob_thread = _bind_cold_sweep_task_link(bob_home.home_dir, task_id="task-bob-active", status="active")
        _insert_cold_sweep_runtime_run(
            bob_home.home_dir,
            task_run_id="tr-bob-active",
            task_id="task-bob-active",
            task_status="running",
            agent_run_id="ar-bob-active",
            run_id="run-bob-active",
            agent_status="running",
            attempt_id="att-bob-active",
            attempt_status="running",
            settled_at=settled_at,
        )
        bob_store = _write_cold_sweep_process_session(
            bob_home.home_dir,
            session_id="bg-bob-active",
            thread_id=bob_thread,
            root_task_id="task-bob-active",
            run_id="run-bob-active",
            attempt_id="att-bob-active",
            pid=bob_active_proc.pid,
            started_at=settled_at,
        )

        bob_unknown_proc = _spawn_cold_sweep_sleeper()
        procs.append(bob_unknown_proc)
        _write_cold_sweep_process_session(
            bob_home.home_dir,
            session_id="bg-bob-unknown",
            thread_id="t-bob-unknown",
            root_task_id="task-bob-unknown",
            run_id="run-bob-unknown",
            attempt_id="att-bob-unknown",
            pid=bob_unknown_proc.pid,
            started_at=settled_at,
        )

        provider_proc = _spawn_cold_sweep_sleeper()
        procs.append(provider_proc)
        provider_thread = _bind_cold_sweep_task_link(
            provider_home.home_dir, task_id="task-provider", status="completed",
        )
        _insert_cold_sweep_runtime_run(
            provider_home.home_dir,
            task_run_id="tr-provider",
            task_id="task-provider",
            task_status="done",
            agent_run_id="ar-provider",
            run_id="run-provider",
            agent_status="done",
            attempt_id="att-provider",
            attempt_status="done",
            settled_at=settled_at,
        )
        provider_store = _write_cold_sweep_process_session(
            provider_home.home_dir,
            session_id="bg-provider-settled",
            thread_id=provider_thread,
            root_task_id="task-provider",
            run_id="run-provider",
            attempt_id="att-provider",
            pid=provider_proc.pid,
            started_at=settled_at,
        )

        assert (base_home.root / "owners" / "local" / ".my-agent-runtime").is_dir()
        assert (base_home.root / "owners" / "providers" / "feishu" / "users" / ".my-agent-runtime").is_dir()

        agent = SimpleNamespace(
            root=base_home.workspace_dir,
            config=AgentConfig(background_process_settled_ttl_seconds=300),
            home_paths=base_home,
        )
        expected_store_root = gateway_process._background_store_root(agent)
        targets = gateway_process._discover_cold_start_owner_targets(agent)
        expected_targets = [
            (str(main_home.home_dir.resolve()), expected_store_root),
            (str(alice_home.home_dir.resolve()), None),
            (str(bob_home.home_dir.resolve()), None),
            (str(provider_home.home_dir.resolve()), None),
        ]
        assert targets == expected_targets
        assert len({home for home, _ in targets}) == 4

        assert gateway_process._resolve_cold_start_settled_sweep_ttl(
            SimpleNamespace(config=AgentConfig(background_process_settled_ttl_seconds=0)),
            args=SimpleNamespace(after_pid=0),
            restart_marker=None,
        ) == 0
        assert gateway_process._resolve_cold_start_settled_sweep_ttl(
            agent,
            args=SimpleNamespace(after_pid=0),
            restart_marker={"request_id": "req-1"},
        ) == 0
        assert gateway_process._resolve_cold_start_settled_sweep_ttl(
            agent,
            args=SimpleNamespace(after_pid=4321),
            restart_marker=None,
        ) == 0

        noop_state: dict[str, object] = {}
        assert gateway_process.run_cold_start_settled_sweep_step(
            agent, noop_state, ttl_seconds=0, max_owners_per_tick=2, slice_ms=0,
        ) is True
        assert noop_state["done"] is True
        assert "targets" not in noop_state
        assert alice_proc.poll() is None
        assert provider_proc.poll() is None

        visited: list[tuple[str, str | None]] = []
        events: list[tuple[str, dict[str, object]]] = []
        real_freeze = gateway_process.freeze_settled_leftover_processes

        def _spy_freeze(owner_home: str | Path, **kwargs):
            visited.append((str(owner_home), kwargs.get("store_root")))
            return real_freeze(owner_home, **kwargs)

        state: dict[str, object] = {}
        with (
            patch("agent_py_agent.cli.gateway_process.freeze_settled_leftover_processes", side_effect=_spy_freeze),
            patch(
                "agent_py_agent.cli.gateway_process.log_gateway_event",
                side_effect=lambda _a, name, event_data: events.append((name, event_data)),
            ),
        ):
            assert gateway_process.run_cold_start_settled_sweep_step(
                agent, state, ttl_seconds=300, max_owners_per_tick=2, slice_ms=0,
            ) is False
            assert len(visited) == 2
            assert events == []
            assert gateway_process.run_cold_start_settled_sweep_step(
                agent, state, ttl_seconds=300, max_owners_per_tick=2, slice_ms=0,
            ) is True

        assert visited == expected_targets
        assert state["done"] is True
        assert len(events) == 1
        assert events[0][0] == "gateway_settled_background_sweep"
        assert events[0][1]["ttl_seconds"] == 300
        assert events[0][1]["active"] == 1
        assert events[0][1]["settled"] == 2
        assert events[0][1]["stopped"] == 2
        assert events[0][1]["unknown"] == 1
        assert events[0][1]["stop_failed"] == 0
        assert events[0][1]["errors"] == []

        alice_proc.wait(timeout=5)
        provider_proc.wait(timeout=5)
        assert alice_store.load("bg-alice-settled").record["status"] in PROCESS_TERMINAL_STATUSES
        assert provider_store.load("bg-provider-settled").record["status"] in PROCESS_TERMINAL_STATUSES
        assert bob_active_proc.poll() is None
        assert bob_unknown_proc.poll() is None
        assert bob_store.load("bg-bob-active").record["status"] == "running"
        assert bob_store.load("bg-bob-unknown").record["status"] == "running"
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def test_fw41r2_cold_start_owner_discovery_handles_dot_users_and_non_main_primary(tmp_path: Path) -> None:
    """Verify canonical dot-containing local users, non-main primary dedup, and symlink exclusions."""
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home, home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        ensure_owner_home,
        home_paths_with_owner,
    )
    from agent_py_agent.cli import gateway_process

    base_home = ensure_my_agent_home(tmp_path / "home")
    main_home = ensure_owner_home(base_home.root, OwnerIdentity.local_main())
    alice_home = ensure_owner_home(base_home.root, OwnerIdentity.local_user("alice"))
    dot_user_home = ensure_owner_home(base_home.root, OwnerIdentity.local_user("user.1"))
    group_home = ensure_owner_home(base_home.root, OwnerIdentity.provider_group("feishu", "oc-group-1"))

    outside = (tmp_path / "outside").resolve()
    (outside / "users" / "escaped").mkdir(parents=True)
    feishu_users_dir = base_home.root / "owners" / "providers" / "feishu" / "users"
    feishu_users_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(
        outside,
        feishu_users_dir / "linked_puser",
        target_is_directory=True,
    )
    os.symlink(
        outside,
        base_home.root / "owners" / "providers" / "sym_provider",
        target_is_directory=True,
    )
    (base_home.root / "owners" / "local" / ".hidden_dot").mkdir(parents=True)
    (base_home.root / "owners" / "providers" / "feishu" / "groups" / ".my-agent-runtime").mkdir(parents=True)

    alice_paths = home_paths_with_owner(home_paths(base_home.root), alice_home)
    agent = SimpleNamespace(
        root=base_home.workspace_dir,
        config=AgentConfig(background_process_settled_ttl_seconds=300),
        home_paths=alice_paths,
    )
    expected_store_root = gateway_process._background_store_root(agent)
    assert gateway_process._discover_cold_start_owner_targets(agent) == [
        (str(alice_home.home_dir.resolve()), expected_store_root),
        (str(main_home.home_dir.resolve()), None),
        (str(dot_user_home.home_dir.resolve()), None),
        (str(group_home.home_dir.resolve()), None),
    ]
