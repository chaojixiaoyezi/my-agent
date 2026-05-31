from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# LLM: home maintenance tests cover migration, doctor, and retention without adding runtime hard gates.
# 模块用途: 验证 owner home 的搬家、体检和清理计划都是可审计、非破坏性、按配置工作的。


def test_home_migration_copies_legacy_memory_raw_and_hooks_to_owner(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.home_migration import (
        apply_home_migration,
        plan_home_migration,
    )

    home = ensure_my_agent_home(tmp_path)
    legacy_memory = home.data_dir / "memory.jsonl"
    legacy_memory.parent.mkdir(parents=True, exist_ok=True)
    legacy_memory.write_text('{"role":"user","content":"legacy"}\n', encoding="utf-8")
    (home.memory_raw_dir / "2026-05-01.jsonl").write_text('{"raw":1}\n', encoding="utf-8")
    (home.memory_hooks_dir / "2026-05-01.jsonl").write_text('{"hook":1}\n', encoding="utf-8")

    plan = plan_home_migration(home)
    assert {action.action for action in plan.actions} >= {"copy_long_term_memory", "copy_raw_memory", "copy_hook_memory"}

    result = apply_home_migration(home)

    statuses = {action.action: action.status for action in result.actions}
    assert statuses["copy_long_term_memory"] == "copied"
    assert (home.owner_memory_long_term_dir / "memory.jsonl").read_text(encoding="utf-8") == legacy_memory.read_text(encoding="utf-8")
    assert (home.owner_memory_raw_dir / "2026-05-01.jsonl").exists()
    assert (home.owner_memory_hooks_dir / "2026-05-01.jsonl").exists()
    assert legacy_memory.exists()


def test_home_doctor_reports_migration_dangling_index_and_retention_advice(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_doctor import build_home_doctor_report
    from agent_py_agent.agent.user_space.home_indexes import TaskIndexRef, register_task_ref
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    (home.memory_raw_dir / "2026-05-01.jsonl").write_text("{}\n", encoding="utf-8")
    register_task_ref(home, TaskIndexRef(owner_id=home.owner_id, task_id="missing-task", task_path=home.owner_tasks_dir / "missing", status="running"))

    old_cache = home.owner_cache_dir / "old.tmp"
    old_cache.write_text("cache", encoding="utf-8")
    _set_mtime(old_cache, "2025-01-01T00:00:00+00:00")

    report = build_home_doctor_report(home)

    assert report["ok"] is True
    assert report["migration"]["pending_count"] >= 1
    assert report["indexes"]["dangling_count"] == 1
    assert report["retention"]["planned_count"] >= 1
    assert any(item["kind"] == "dangling_index" for item in report["findings"])


def test_owner_retention_plan_and_apply_delete_only_expired_files(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.home_retention import (
        apply_owner_retention,
        plan_owner_retention,
    )

    home = ensure_my_agent_home(tmp_path)
    old_raw = home.owner_memory_raw_dir / "old.jsonl"
    fresh_raw = home.owner_memory_raw_dir / "fresh.jsonl"
    old_raw.write_text("old\n", encoding="utf-8")
    fresh_raw.write_text("fresh\n", encoding="utf-8")
    _set_mtime(old_raw, "2025-01-01T00:00:00+00:00")
    _set_mtime(fresh_raw, "2026-05-30T00:00:00+00:00")

    retention = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    retention["raw_days"] = 30
    home.owner_retention_json.write_text(json.dumps(retention, ensure_ascii=False), encoding="utf-8")

    plan = plan_owner_retention(home, now=datetime(2026, 5, 31, tzinfo=timezone.utc))
    assert [action.path for action in plan.actions] == [old_raw]
    assert old_raw.exists()

    applied = apply_owner_retention(home, now=datetime(2026, 5, 31, tzinfo=timezone.utc))

    assert applied.applied is True
    assert not old_raw.exists()
    assert fresh_raw.exists()
    audit_rows = [
        json.loads(line)
        for line in home.owner_audit_log_jsonl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit_rows[-1]["event_type"] == "owner_retention_applied"
    assert audit_rows[-1]["actions"][0]["path"] == str(old_raw)


def _set_mtime(path: Path, iso: str) -> None:
    timestamp = datetime.fromisoformat(iso).timestamp()
    os.utime(path, (timestamp, timestamp))
