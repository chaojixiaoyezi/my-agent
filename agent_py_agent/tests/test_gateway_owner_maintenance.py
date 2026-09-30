from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace


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

    # failed 仍是旧口径（两个都算）；refused 与 isolated 把「整次被拒」和「执行了、隔离了」分开。
    assert summary == {"scanned": 2, "ran": 3, "failed": 2, "refused": 1, "isolated": 1}
    printed = [line for line in capsys.readouterr().out.splitlines() if line.startswith("[gateway-owner-maintenance] ")]
    assert json.loads(printed[-1].split(" ", 1)[1]) == summary
