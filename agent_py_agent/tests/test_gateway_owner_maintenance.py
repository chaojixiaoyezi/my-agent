from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

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
    tmp_path: Path, monkeypatch, capsys, maintenance_clock,
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


def test_gateway_owner_maintenance_isolated_only_is_not_failed(tmp_path: Path, monkeypatch, maintenance_clock) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    base_home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(base_home.root, OwnerIdentity.provider_user("feishu", "ou-isolated-only"))
    work = owner.home_dir / "tasks" / "2026-01-01" / "broken-task" / "work"
    work.mkdir(parents=True)
    (work / "state.json").write_text("{broken", encoding="utf-8")

    summary = _tick_after_startup_grace(_controller_for(base_home, monkeypatch), maintenance_clock)

    assert summary == {"scanned": 1, "ran": 2, "failed": 0, "refused": 0, "isolated": 1}
    # 持久化的 status 仍按旧口径记 policy_unavailable，摘要不影响它。
    marker = json.loads((owner.data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert (marker["status"], marker["apply_outcome"]) == ("policy_unavailable", "applied")


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
