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
    assert (first.apply_outcome, first.isolated_error_count) == ("applied", 0)
    assert not old_cache.exists()
    assert throttled.to_dict() == {"ran": False, "status": "not_due"}
    assert next_run.ran is True
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert marker["last_attempt_at"] == 200_101
    assert marker["status"] == "success"
    assert (marker["apply_outcome"], marker["isolated_error_count"], marker["last_applied_at"]) == ("applied", 0, 200_101)


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
    assert result.to_dict()["apply_outcome"] == "refused"
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert marker["load_errors"]
    assert marker["last_success_at"] == 0.0
    assert (marker["apply_outcome"], marker["isolated_error_count"], marker["last_applied_at"]) == ("refused", 0, 0.0)
    event = _retention_events(home)[-1]
    assert (event["applied"], event["isolated_error_count"], event["isolated_error_codes"]) == (False, 0, {})


def _retention_events(home) -> list[dict]:
    lines = Path(home.owner_audit_log_jsonl).read_text(encoding="utf-8").splitlines()
    return [row for row in map(json.loads, lines) if row.get("event_type") == "owner_retention_applied"]


def _home_with_old_cache(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    old_cache = home.owner_cache_dir / "old.tmp"
    old_cache.write_text("old", encoding="utf-8")
    os.utime(old_cache, (1, 1))
    policy = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    policy.update({"cache_days": 1, "maintenance_interval_seconds": 100})
    home.owner_retention_json.write_text(json.dumps(policy), encoding="utf-8")
    return home, old_cache


def _break_task_state(home) -> None:
    work = Path(home.owner_tasks_dir) / "2026-01-01" / "broken-task" / "work"
    work.mkdir(parents=True)
    (work / "state.json").write_text("{broken", encoding="utf-8")


def test_applied_run_with_isolated_errors_is_not_masked(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home, old_cache = _home_with_old_cache(tmp_path)
    _break_task_state(home)

    result = run_owner_retention_if_due(home, now=200_000)

    # 旧口径不变：有路径级错误就还是 policy_unavailable，last_success_at 不前进。
    assert result.status == "policy_unavailable"
    # 新字段说清执行事实：执行了、有被隔离的错误，其余动作照常执行。计数是错误条数、不是子树数：
    #   同一个坏 state.json 会被 completed_task 与 tool_output 两个扫描器各报一次（既有行为），所以是 2。
    assert (result.apply_outcome, result.isolated_error_count) == ("applied", 2)
    assert not old_cache.exists()
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert (marker["status"], marker["last_success_at"]) == ("policy_unavailable", 0.0)
    assert (marker["apply_outcome"], marker["isolated_error_count"], marker["last_applied_at"]) == ("applied", 2, 200_000)
    assert [error["error_code"] for error in marker["load_errors"]] == ["MEMORY_RETENTION_TASK_STATE_INVALID"] * 2
    assert [error["error_code"] for error in result.retention.to_dict()["isolated_errors"]] == [
        "MEMORY_RETENTION_TASK_STATE_INVALID"] * 2
    event = _retention_events(home)[-1]
    assert (event["applied"], event["ok"], event["errors"]) == (True, True, [])
    assert (event["isolated_error_count"], event["isolated_error_codes"]) == (2, {"MEMORY_RETENTION_TASK_STATE_INVALID": 2})


def test_last_applied_at_survives_a_refused_run(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home, _old_cache = _home_with_old_cache(tmp_path)
    assert run_owner_retention_if_due(home, now=200_000).apply_outcome == "applied"
    home.owner_retention_json.write_text("{broken", encoding="utf-8")

    # 策略读不了时到期判断按默认 86400 秒。
    result = run_owner_retention_if_due(home, now=200_000 + 86_400)

    assert (result.status, result.apply_outcome) == ("policy_unavailable", "refused")
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert (marker["last_attempt_at"], marker["apply_outcome"], marker["last_applied_at"]) == (286_400, "refused", 200_000)
    assert marker["isolated_error_count"] == 0


def test_legal_hold_outcome_is_explicit(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home, old_cache = _home_with_old_cache(tmp_path)
    policy = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    policy["legal_hold"] = True
    home.owner_retention_json.write_text(json.dumps(policy), encoding="utf-8")

    result = run_owner_retention_if_due(home, now=200_000)

    assert (result.status, result.apply_outcome, result.isolated_error_count) == ("legal_hold", "legal_hold", 0)
    assert old_cache.exists()
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert (marker["apply_outcome"], marker["last_applied_at"]) == ("legal_hold", 0.0)
