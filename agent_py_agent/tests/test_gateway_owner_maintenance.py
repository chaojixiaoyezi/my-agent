from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


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

    controller = gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace())
    summary = controller.tick(now=200_000)

    assert summary == {"scanned": 1, "ran": 2, "failed": 0, "refused": 0, "isolated": 0}
    assert not old_cache.exists()
    marker = json.loads((owner.data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert marker["status"] == "success"


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

    summary = gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace()).tick(now=200_000)

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
