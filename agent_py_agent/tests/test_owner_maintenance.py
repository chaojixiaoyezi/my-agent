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
    #   解析不了的坏 state.json 会被 completed_task 与 tool_output 两个扫描器各报一次（既有行为），所以是 2；
    #   能解析、只缺 status 的旧格式只报一次。
    assert (result.apply_outcome, result.isolated_error_count, result.failed_action_count) == ("applied", 2, 0)
    assert result.to_dict()["failed_action_count"] == 0
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


def test_perf_e_owner_sidecar_cleanup_preserves_logical_and_live_locks(tmp_path):
    from agent_py_agent.agent.gateway_parts.io import locked_file_transition
    from agent_py_agent.agent.user_space import owner_maintenance as module
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    reclaim = getattr(module, "reclaim_owner_orphan_lock_files", None)
    assert callable(reclaim), "missing owner sidecar maintenance"
    orphan = home.owner_memory_dir / "gone.jsonl.lock"
    present = home.owner_memory_dir / "present.json.lock"
    logical = home.owner_memory_dir / ".owner-quota.lock"
    active = home.owner_memory_dir / "active.json"
    fresh = home.owner_memory_dir / "fresh.json.lock"
    excluded = home.root / "local_users" / "operations" / "op.lock"
    excluded.parent.mkdir(parents=True, exist_ok=True)
    for path in (orphan, present, logical, excluded):
        path.write_text("")
        os.utime(path, (1, 1))
    present.with_suffix("").write_text("{}")
    fresh.write_text("")
    with locked_file_transition(active):
        active_lock = active.with_suffix(".json.lock")
        os.utime(active_lock, (1, 1))
        assert reclaim(home) == 1
        assert active_lock.exists()
    assert not orphan.exists()
    assert all(path.exists() for path in (logical, present, fresh, excluded))


def test_perf_e_owner_retention_reclaims_sidecars_after_apply(tmp_path):
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home, _cache = _home_with_old_cache(tmp_path)
    orphan = home.owner_memory_dir / "retired.json.lock"
    orphan.write_text("")
    os.utime(orphan, (1, 1))
    assert run_owner_retention_if_due(home, now=200_000).ran
    assert not orphan.exists()


def test_perf_e_sidecar_registry_is_exact_and_never_creates_paths(tmp_path):
    from agent_py_agent.agent.user_space import owner_maintenance as module
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    registry = getattr(module, "owner_lock_sidecar_dirs", None)
    assert callable(registry), "missing registered sidecar directories"
    expected = (home.owner_memory_dir, home.owner_memory_daily_dir, home.owner_memory_curator_runs_dir,
        home.owner_compact_dir, home.owner_cache_dir, home.owner_tmp_dir, home.owner_scheduler_dir,
        *(home.owner_data_dir / name for name in ("decision", "context", "skill_learning", "skill_proposals")))
    assert registry(home) == expected
    before = {path for path in expected if path.exists()}
    module.reclaim_owner_orphan_lock_files(home)
    assert {path for path in expected if path.exists()} == before


def test_perf_e_cli_maintenance_runs_once_and_skips_subagent(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.runtime_context import (
        restore_current_subagent_context,
        set_current_subagent_context,
    )
    from agent_py_agent.agent.user_space import owner_maintenance as module

    home, cache = _home_with_old_cache(tmp_path)
    wrapper = getattr(module, "maybe_run_cli_owner_maintenance", None)
    assert callable(wrapper), "missing safe CLI maintenance wrapper"
    agent = SimpleNamespace(home_paths=home)
    previous = set_current_subagent_context(agent, run_id="child")
    try:
        assert wrapper(agent, now=200_000) is None and cache.exists()
    finally:
        restore_current_subagent_context(agent, previous)
    result = wrapper(agent, now=200_000)
    assert result.ran and not cache.exists()
    assert wrapper(agent, now=200_001) is None
    assert wrapper(SimpleNamespace(home_paths=home.owner_home_dir), now=200_000) is None


def test_perf_e_cli_maintenance_refuses_restricted_or_missing_execution(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts import request_entry_admission
    from agent_py_agent.agent.user_space import owner_maintenance as module

    home, cache = _home_with_old_cache(tmp_path)
    wrapper = getattr(module, "maybe_run_cli_owner_maintenance", None)
    assert callable(wrapper), "missing safe CLI maintenance wrapper"
    monkeypatch.setattr(request_entry_admission, "request_execution_required", lambda: True)
    assert wrapper(SimpleNamespace(home_paths=home), now=200_000) is None
    monkeypatch.setattr(request_entry_admission, "request_execution_required", lambda: False)

    def refuse(**kwargs):
        raise RuntimeError("execution unavailable")

    monkeypatch.setattr(request_entry_admission, "require_request_execution", refuse)
    assert wrapper(SimpleNamespace(home_paths=home), now=200_000) is None
    assert cache.exists()


def test_perf_e_plain_local_completion_calls_safe_maintenance(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space import owner_maintenance
    from agent_py_agent.cli.chat_parts import plain_handlers as module

    called = []
    monkeypatch.setattr(owner_maintenance, "maybe_run_cli_owner_maintenance",
        lambda agent: called.append(agent), raising=False)
    result = SimpleNamespace(response="done", runtime_status="ok")
    agent = SimpleNamespace(run=lambda *args, **kwargs: result, config=SimpleNamespace(agent_name="test"))
    job = SimpleNamespace(request_id="local-test", user="test", system_task=False, prompt_files=[])
    ctx = module.PlainJobContext(job, agent, SimpleNamespace(no_save=True), None, [], lambda: "",
        local_run=SimpleNamespace(request_id=job.request_id, check_admission=lambda: None))
    monkeypatch.setattr(module, "_make_chunk_handler", lambda *args, **kwargs: (None, [0]))
    monkeypatch.setattr(module, "_turn_inject", lambda *args, **kwargs: [])
    monkeypatch.setattr(module, "resume_context_override", lambda args: None)
    monkeypatch.setattr(module, "_render_if_needed", lambda *args: None)
    monkeypatch.setattr(module, "_print_local_timing", lambda *args: None)
    assert module._plain_local_handle(ctx)[0] == "done"
    assert called == [agent]


def test_perf_e_owner_cleanup_pins_directory_before_ancestor_replacement(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import io
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import reclaim_owner_orphan_lock_files

    home = ensure_my_agent_home(tmp_path / "home")
    external = tmp_path / "external"
    (external / "daily").mkdir(parents=True)
    sentinel = external / "daily" / "external.json.lock"
    sentinel.write_text("")
    os.utime(sentinel, (1, 1))
    actual = io.cleanup_orphan_lock_files
    swapped = []

    def switch(directory, **kwargs):
        if directory == home.owner_memory_daily_dir and not swapped:
            home.owner_memory_dir.rename(home.owner_home_dir / "moved-memory")
            home.owner_memory_dir.symlink_to(external, target_is_directory=True)
            swapped.append(True)
        return actual(directory, **kwargs)

    monkeypatch.setattr(io, "cleanup_orphan_lock_files", switch)
    reclaim_owner_orphan_lock_files(home)
    assert swapped and sentinel.exists()


def test_perf_e_orphan_cleanup_closes_raw_fd_when_fdopen_fails(tmp_path, monkeypatch):
    import time

    import pytest

    from agent_py_agent.agent.gateway_parts import io

    lock = tmp_path / "old.json.lock"
    lock.write_text("")
    os.utime(lock, (1, 1))
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    opened = []

    def refuse(fd, *args, **kwargs):
        opened.append(fd)
        raise OSError("fdopen failed before ownership transfer")

    monkeypatch.setattr(io.os, "fdopen", refuse)
    try:
        assert not io._cleanup_orphan_lock_at(directory_fd, lock.name, current=time.time())
        assert opened and lock.exists()
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        for fd in [*opened, directory_fd]:
            try:
                os.close(fd)
            except OSError:
                pass
