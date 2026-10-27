from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


# LLM: 业务分页用例观察保护期结束后的真实tick，不删除或替换生产启动保护判定。
# 函数用途: 验证后台维护按owner分页运行而不创建scoped agent。
def test_gateway_owner_maintenance_pages_without_creating_scoped_agents(
    tmp_path: Path,
    monkeypatch,
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

    controller = _after_startup_grace(gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace()))
    summary = controller.tick(now=200_000)

    assert summary == {"scanned": 1, "ran": 2, "failed": 0, "refused": 0, "isolated": 0}
    assert not old_cache.exists()
    marker = json.loads((owner.data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert marker["status"] == "success"


# LLM: 只推进测试观测到原180秒保护期之后，保留全部汇总和实际执行断言。
# 函数用途: 验证拒绝与局部隔离错误分别进入正确的维护摘要计数。
def test_gateway_owner_maintenance_summary_separates_refused_from_isolated(tmp_path: Path, monkeypatch, capsys) -> None:
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

    controller = _after_startup_grace(gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace()))
    summary = controller.tick(now=200_000)

    # failed 只算整次被拒与执行期失败；「执行了、只有隔离错误」只进 isolated（3a 补充裁定）。
    assert summary == {"scanned": 2, "ran": 3, "failed": 1, "refused": 1, "isolated": 1}
    printed = [line for line in capsys.readouterr().out.splitlines() if line.startswith("[gateway-owner-maintenance] ")]
    assert json.loads(printed[-1].split(" ", 1)[1]) == summary


# LLM: 只改变当前测试controller的出生时钟，不修改生产常量、判定函数或其它controller。
# 函数用途: 将维护业务回归的观测点置于原启动保护结束后。
def _after_startup_grace(controller):
    from agent_py_agent.cli import gateway_loops

    controller._startup_started_at -= gateway_loops.STARTUP_GRACE_WINDOW_SECONDS + 1
    assert not gateway_loops._startup_grace_active(controller)
    return controller


# LLM: 真实controller沿原基准home构造；业务用例统一在保护期结束后观察，不替换tick或维护写者。
# 函数用途: 为隔离错误和执行失败测试准备到达稳态的维护controller。
def _controller_for(base_home, monkeypatch):
    from agent_py_agent.cli import gateway_loops

    base_agent = SimpleNamespace(
        config=SimpleNamespace(owner_maintenance_scan_interval_seconds=60, owner_agent_pool_max_agents=64),
        home_paths=base_home,
    )
    monkeypatch.setattr(gateway_loops, "_gateway_agent_from_context", lambda _context: base_agent)
    return _after_startup_grace(gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace()))


def test_gateway_owner_maintenance_isolated_only_is_not_failed(tmp_path: Path, monkeypatch) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    base_home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(base_home.root, OwnerIdentity.provider_user("feishu", "ou-isolated-only"))
    work = owner.home_dir / "tasks" / "2026-01-01" / "broken-task" / "work"
    work.mkdir(parents=True)
    (work / "state.json").write_text("{broken", encoding="utf-8")

    summary = _controller_for(base_home, monkeypatch).tick(now=200_000)

    assert summary == {"scanned": 1, "ran": 2, "failed": 0, "refused": 0, "isolated": 1}
    # 持久化的 status 仍按旧口径记 policy_unavailable，摘要不影响它。
    marker = json.loads((owner.data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert (marker["status"], marker["apply_outcome"]) == ("policy_unavailable", "applied")


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 可以删只读目录里的文件，造不出执行期失败")
def test_gateway_owner_maintenance_counts_execution_failures_as_failed(tmp_path: Path, monkeypatch) -> None:
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
        summary = _controller_for(base_home, monkeypatch).tick(now=200_000)
    finally:
        owner.cache_dir.chmod(0o700)

    assert old_cache.exists()
    assert summary == {"scanned": 1, "ran": 2, "failed": 1, "refused": 0, "isolated": 0}
    marker = json.loads((owner.data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert (marker["apply_outcome"], marker["failed_action_count"], marker["isolated_error_count"]) == ("applied", 1, 0)
