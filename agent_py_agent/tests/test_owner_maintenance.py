from __future__ import annotations

import json
import os
from pathlib import Path


def test_owner_maintenance_applies_once_per_configured_interval(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home = ensure_my_agent_home(tmp_path)
    old_cache = home.owner_cache_dir / "old.tmp"
    old_cache.write_text("old", encoding="utf-8")
    os.utime(old_cache, (1, 1))
    policy = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    policy.update({"cache_days": 1, "maintenance_interval_seconds": 100})
    home.owner_retention_json.write_text(json.dumps(policy), encoding="utf-8")

    first = run_owner_retention_if_due(home, now=200_000)
    throttled = run_owner_retention_if_due(home, now=200_050)
    next_run = run_owner_retention_if_due(home, now=200_101)

    assert first.ran is True
    assert first.status == "success"
    assert not old_cache.exists()
    assert throttled.to_dict() == {"ran": False, "status": "not_due"}
    assert next_run.ran is True
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert marker["last_attempt_at"] == 200_101
    assert marker["status"] == "success"


def test_owner_maintenance_can_be_disabled_by_structured_policy(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import (
        owner_maintenance_due,
        run_owner_retention_if_due,
    )

    home = ensure_my_agent_home(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    policy["maintenance_enabled"] = False
    home.owner_retention_json.write_text(json.dumps(policy), encoding="utf-8")

    assert owner_maintenance_due(home.owner_home_dir, now=200_000) is False
    assert run_owner_retention_if_due(home, now=200_000).status == "not_due"
    assert not (home.owner_data_dir / "maintenance.json").exists()


def test_owner_maintenance_records_fail_closed_policy_error(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home = ensure_my_agent_home(tmp_path)
    home.owner_retention_json.write_text("{broken", encoding="utf-8")

    result = run_owner_retention_if_due(home, now=200_000)

    assert result.ran is True
    assert result.status == "policy_unavailable"
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert marker["load_errors"]
    assert marker["last_success_at"] == 0.0
