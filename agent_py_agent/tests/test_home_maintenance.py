from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _write_config(tmp_path: Path, home: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        f'workspace_root: "{tmp_path / "workspace"}"\n'
        f'my_agent_home: "{home}"\n'
        'model_backend: "echo"\n'
        'prompt_files: []\n',
        encoding="utf-8",
    )
    return config_path


def _run_cli_json(capsys, config_path: Path, *argv: str) -> tuple[int, dict]:
    from agent_py_agent.__main__ import build_parser

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), *argv, "--json"])
    code = args.func(args)
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


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
    assert report["repair_plan"]["auto_repair_count"] >= 3
    commands = {item["command"] for item in report["repair_plan"]["auto_repair"]}
    assert "my-agent home-index-rebuild --apply" in commands
    assert "my-agent home-retention --apply" in commands


def test_home_doctor_reports_backup_requests_and_grants(tmp_path: Path) -> None:
    from datetime import timedelta

    from agent_py_agent.agent.user_space.capability_requests import (
        CreateCapabilityRequest,
        create_capability_request,
    )
    from agent_py_agent.agent.user_space.home_backup import create_home_backup_snapshot
    from agent_py_agent.agent.user_space.home_doctor import build_home_doctor_report
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.temporary_grants import (
        CreateTemporaryGrant,
        create_temporary_grant,
    )

    home = ensure_my_agent_home(tmp_path)
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    create_home_backup_snapshot(home, reason="doctor")
    create_capability_request(
        home,
        CreateCapabilityRequest(
            requested_by="agent-1",
            capability="tool:browser",
            reason="需要网页检查",
            expires_at=(now + timedelta(hours=1)).isoformat(),
        ),
    )
    create_temporary_grant(
        home,
        CreateTemporaryGrant(
            granted_to="agent-1",
            capability="filesystem.write",
            path_prefix=str(home.owner_workspace_dir),
            expires_at=(now + timedelta(hours=1)).isoformat(),
        ),
    )

    report = build_home_doctor_report(home)

    assert report["backup"]["snapshot_count"] == 1
    assert report["capability_requests"]["open_count"] == 1
    assert report["temporary_grants"]["active_count"] == 1


def test_home_doctor_reports_dangling_owner_compact_indexes(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_doctor import build_home_doctor_report
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    pointer = home.owner_compact_dir / "by_task" / "task-a.json"
    pointer.write_text(
        json.dumps(
            {
                "schema_version": "owner-compact-index.v1",
                "task_id": "task-a",
                "rollup_json": str(home.owner_tasks_dir / "missing" / "work" / "compact" / "task_rollup.json"),
                "compact_package": str(home.owner_tasks_dir / "missing" / "work" / "compact" / "compact_0001"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = build_home_doctor_report(home)

    assert report["compact_indexes"]["dangling_count"] == 2
    assert any(item["kind"] == "dangling_compact_index" for item in report["findings"])
    assert report["repair_plan"]["manual_count"] >= 2


def test_home_doctor_reports_corrupt_owner_compact_index_pointer(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_doctor import build_home_doctor_report
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    pointer = home.owner_compact_dir / "by_task" / "bad.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text("{bad-json}\n", encoding="utf-8")

    report = build_home_doctor_report(home)

    assert report["compact_indexes"]["load_errors"]
    assert report["compact_indexes"]["load_errors"][0]["context"] == "owner_compact_index.pointer"
    assert report["compact_indexes"]["load_errors"][0]["path"] == str(pointer)
    finding = next(item for item in report["findings"] if item["kind"] == "compact_index_pointer_unreadable")
    assert finding["path"] == str(pointer)
    assert finding["resolution"]["action_class"] == "manual"
    assert report["repair_plan"]["manual_count"] >= 1


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


def test_home_retention_cli_plans_and_applies_owner_cleanup(tmp_path: Path, capsys) -> None:
    home = tmp_path / "home"
    config_path = _write_config(tmp_path, home)
    old_cache = home / "owners" / "local" / "main" / "cache" / "old.tmp"
    old_cache.parent.mkdir(parents=True, exist_ok=True)
    old_cache.write_text("cache", encoding="utf-8")
    _set_mtime(old_cache, "2025-01-01T00:00:00+00:00")

    code, payload = _run_cli_json(capsys, config_path, "home-retention")

    assert code == 0
    assert payload["retention"]["applied"] is False
    assert payload["retention"]["actions"][0]["path"] == str(old_cache)
    assert old_cache.exists()

    code, applied = _run_cli_json(capsys, config_path, "home-retention", "--apply")

    assert code == 0
    assert applied["retention"]["applied"] is True
    assert applied["retention"]["actions"][0]["status"] == "deleted"
    assert not old_cache.exists()


def test_home_index_rebuild_recreates_task_run_and_agent_refs(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_index_rebuild import rebuild_home_indexes
    from agent_py_agent.agent.user_space.home_indexes import (
        latest_agent_refs,
        latest_run_refs,
        latest_task_refs,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.run_workspace import (
        EnsureRunWorkspaceRequest,
        ensure_run_workspace,
    )

    home = ensure_my_agent_home(tmp_path)
    ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=home.owner_home_dir,
            template="tasks/{date}/{task_slug}",
            task_name="索引恢复任务",
            user_prompt="恢复索引",
            request_id="req-rebuild",
            run_id="run-rebuild",
            task_id="task-rebuild",
            owner_id=home.owner_id,
            owner_home=str(home.owner_home_dir),
            created_at="2026-05-13T01:00:00+00:00",
        )
    )
    agent_dir = home.owner_agents_dir / "agent-rebuild"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "state.json").write_text(
        json.dumps({"id": "agent-rebuild", "task_id": "task-rebuild", "status": "RUNNING"}, ensure_ascii=False),
        encoding="utf-8",
    )

    dry = rebuild_home_indexes(home, apply=False)

    assert dry.applied is False
    assert len(dry.task_refs) == 1
    assert latest_task_refs(home, owner_id=home.owner_id) == []

    applied = rebuild_home_indexes(home, apply=True)

    assert applied.applied is True
    assert [ref["task_id"] for ref in latest_task_refs(home, owner_id=home.owner_id)] == ["task-rebuild"]
    assert [ref["run_id"] for ref in latest_run_refs(home, owner_id=home.owner_id)] == ["run-rebuild"]
    assert [ref["agent_id"] for ref in latest_agent_refs(home, owner_id=home.owner_id)] == ["agent-rebuild"]


def test_home_index_rebuild_reports_corrupt_task_state(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_index_rebuild import rebuild_home_indexes
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    task_root = home.owner_tasks_dir / "2026-06-01" / "bad-task"
    state_path = task_root / "work" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{bad-json}\n", encoding="utf-8")

    result = rebuild_home_indexes(home, apply=False)

    assert result.task_refs[0].task_id == "bad-task"
    assert result.task_refs[0].status == "UNKNOWN"
    assert result.load_errors
    assert result.load_errors[0]["context"] == "home_index_rebuild.task_state"
    assert result.to_dict()["load_errors"] == list(result.load_errors)


def test_home_index_rebuild_cli_defaults_to_dry_run(tmp_path: Path, capsys) -> None:
    config_path = _write_config(tmp_path, tmp_path / "home")

    code, payload = _run_cli_json(capsys, config_path, "home-index-rebuild")

    assert code == 0
    assert payload["index_rebuild"]["applied"] is False


def test_home_index_rebuild_keeps_provider_task_refs_isolated(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_index_rebuild import rebuild_home_indexes
    from agent_py_agent.agent.user_space.home_indexes import latest_run_refs, latest_task_refs
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home
    from agent_py_agent.agent.user_space.run_workspace import (
        EnsureRunWorkspaceRequest,
        ensure_run_workspace,
    )

    home = ensure_my_agent_home(tmp_path)
    owner_a = ensure_owner_home(home.root, OwnerIdentity.provider_user("feishu", "ou_a"))
    owner_b = ensure_owner_home(home.root, OwnerIdentity.provider_user("feishu", "ou_b"))
    for owner, run_id in ((owner_a, "run-a"), (owner_b, "run-b")):
        ensure_run_workspace(
            EnsureRunWorkspaceRequest(
                home=owner.home_dir,
                template="tasks/{date}/{task_slug}",
                task_name="同名任务",
                user_prompt="同名",
                request_id=f"req-{run_id}",
                run_id=run_id,
                task_id="shared-task",
                owner_id=owner.owner_id,
                owner_home=str(owner.home_dir),
                created_at="2026-05-13T01:00:00+00:00",
            )
        )

    rebuild_home_indexes(home, apply=True)

    assert [ref["task_id"] for ref in latest_task_refs(home, owner_id=owner_a.owner_id)] == ["shared-task"]
    assert [ref["task_id"] for ref in latest_task_refs(home, owner_id=owner_b.owner_id)] == ["shared-task"]
    assert [ref["run_id"] for ref in latest_run_refs(home, owner_id=owner_a.owner_id)] == ["run-a"]
    assert [ref["run_id"] for ref in latest_run_refs(home, owner_id=owner_b.owner_id)] == ["run-b"]


def _set_mtime(path: Path, iso: str) -> None:
    timestamp = datetime.fromisoformat(iso).timestamp()
    os.utime(path, (timestamp, timestamp))
