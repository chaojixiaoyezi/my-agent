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
    from agent_py_agent.cli.parser import build_parser

    parser = build_parser()
    args = parser.parse_args(["--config", str(config_path), *argv, "--json"])
    code = args.func(args)
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def test_home_doctor_reports_dangling_index_and_retention_advice(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_doctor import build_home_doctor_report
    from agent_py_agent.agent.user_space.home_indexes import TaskIndexRef, register_task_ref
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    legacy_raw_dir = home.root / "memory" / "raw"
    legacy_raw_dir.mkdir(parents=True, exist_ok=True)
    (legacy_raw_dir / "2026-05-01.jsonl").write_text("{}\n", encoding="utf-8")
    register_task_ref(home, TaskIndexRef(owner_id=home.owner_id, task_id="missing-task", task_path=home.owner_tasks_dir / "missing", status="running"))

    old_cache = home.owner_cache_dir / "old.tmp"
    old_cache.write_text("cache", encoding="utf-8")
    _set_mtime(old_cache, "2025-01-01T00:00:00+00:00")

    report = build_home_doctor_report(home)

    assert report["ok"] is True
    assert report["indexes"]["dangling_count"] == 1
    assert report["retention"]["planned_count"] >= 1
    assert any(item["kind"] == "dangling_index" for item in report["findings"])
    assert report["repair_plan"]["auto_repair_count"] >= 2
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


def test_owner_retention_plan_and_apply_delete_only_expired_files(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.home_retention import (
        apply_owner_retention,
        plan_owner_retention,
    )

    home = ensure_my_agent_home(tmp_path)
    old_raw = home.owner_audit_dir / "old.jsonl"
    fresh_raw = home.owner_audit_dir / "fresh.jsonl"
    old_raw.write_text("old\n", encoding="utf-8")
    fresh_raw.write_text("fresh\n", encoding="utf-8")
    _set_mtime(old_raw, "2025-01-01T00:00:00+00:00")
    _set_mtime(fresh_raw, "2026-05-30T00:00:00+00:00")

    retention = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    retention["audit_days"] = 30
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


def test_owner_retention_trashes_only_structured_terminal_task(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.home_retention import apply_owner_retention

    home = ensure_my_agent_home(tmp_path)
    old_done = _task_workspace(home.owner_tasks_dir, "done", "DONE", "2025-01-01T00:00:00+00:00")
    old_active = _task_workspace(
        home.owner_tasks_dir,
        "active",
        "RUNNING",
        "2025-01-01T00:00:00+00:00",
    )
    fresh_done = _task_workspace(home.owner_tasks_dir, "fresh", "DONE", "2026-05-30T00:00:00+00:00")
    _set_retention(home.owner_retention_json, completed_task_days=30)

    applied = apply_owner_retention(home, now=datetime(2026, 5, 31, tzinfo=timezone.utc))

    task_action = next(action for action in applied.actions if action.category == "completed_task")
    assert task_action.status == "trashed"
    assert not old_done.exists()
    assert old_active.exists()
    assert fresh_done.exists()
    assert task_action.destination is not None
    assert (task_action.destination / "payload" / "output" / "result.txt").is_file()
    tombstone = json.loads((task_action.destination / "tombstone.json").read_text(encoding="utf-8"))
    assert tombstone["source_path"] == str(old_done)
    assert tombstone["authority_status"] == "DONE"

    _set_retention(home.owner_retention_json, completed_task_days=10_000, trash_days=30)
    purged = apply_owner_retention(home, now=datetime(2026, 7, 1, tzinfo=timezone.utc))

    purge_action = next(action for action in purged.actions if action.operation == "delete_tree")
    assert purge_action.status == "deleted"
    assert not task_action.destination.exists()


def test_owner_retention_preserves_held_task_and_all_cleanup_on_legal_hold(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.home_retention import apply_owner_retention

    home = ensure_my_agent_home(tmp_path)
    held = _task_workspace(home.owner_tasks_dir, "held", "DONE", "2025-01-01T00:00:00+00:00")
    old_cache = home.owner_cache_dir / "old.tmp"
    old_cache.write_text("old", encoding="utf-8")
    _set_mtime(old_cache, "2025-01-01T00:00:00+00:00")
    _set_retention(
        home.owner_retention_json,
        completed_task_days=30,
        legal_hold=True,
        legal_hold_task_ids=["held"],
    )

    result = apply_owner_retention(home, now=datetime(2026, 5, 31, tzinfo=timezone.utc))

    assert result.applied is False
    assert result.legal_hold is True
    assert result.actions == ()
    assert held.exists()
    assert old_cache.exists()


def test_owner_retention_moves_only_terminal_subagent_scratch(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.home_retention import apply_owner_retention

    home = ensure_my_agent_home(tmp_path)
    task = _task_workspace(home.owner_tasks_dir, "active-task", "RUNNING", "2026-05-30T00:00:00+00:00")
    done_run = _subagent_workspace(task, "done-run", "DONE", 1_735_689_600.0)
    active_run = _subagent_workspace(task, "active-run", "RUNNING", 1_735_689_600.0)
    _set_retention(home.owner_retention_json, subagent_scratch_days=30)

    result = apply_owner_retention(home, now=datetime(2026, 5, 31, tzinfo=timezone.utc))

    scratch_actions = [action for action in result.actions if action.category == "subagent_scratch"]
    assert len(scratch_actions) == 4
    assert all(action.status == "trashed" for action in scratch_actions)
    assert (done_run / "canonical_state.json").is_file()
    assert (done_run / "final_report.md").is_file()
    assert not (done_run / "inbox").exists()
    assert not (done_run / "outbox").exists()
    assert not (done_run / "compactions").exists()
    assert not (done_run / "artifacts" / "tool_outputs").exists()
    assert (active_run / "inbox").exists()


def test_owner_retention_fails_closed_for_corrupt_policy(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.home_retention import apply_owner_retention

    home = ensure_my_agent_home(tmp_path)
    old_cache = home.owner_cache_dir / "old.tmp"
    old_cache.write_text("old", encoding="utf-8")
    _set_mtime(old_cache, "2025-01-01T00:00:00+00:00")
    home.owner_retention_json.write_text("{broken", encoding="utf-8")

    result = apply_owner_retention(home, now=datetime(2026, 5, 31, tzinfo=timezone.utc))

    assert result.applied is False
    assert result.load_errors
    assert old_cache.exists()


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


def _task_workspace(tasks_root: Path, task_id: str, status: str, updated_at: str) -> Path:
    task = tasks_root / "2025-01-01" / task_id
    work = task / "work"
    work.mkdir(parents=True)
    (task / "output").mkdir()
    (task / "output" / "result.txt").write_text(task_id, encoding="utf-8")
    (work / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "task_id": task_id,
                "status": status,
                "updated_at": updated_at,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return task


def _subagent_workspace(task: Path, run_id: str, status: str, updated_at: float) -> Path:
    run = task / "work" / "agents" / run_id
    for relative in ("inbox", "outbox", "compactions", "artifacts/tool_outputs"):
        target = run / relative
        target.mkdir(parents=True, exist_ok=True)
        (target / "scratch.txt").write_text("scratch", encoding="utf-8")
    (run / "final_report.md").write_text("final", encoding="utf-8")
    (run / "canonical_state.json").write_text(
        json.dumps(
            {
                "id": run_id,
                "status": status,
                "updated_at": updated_at,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return run


def _set_retention(path: Path, **updates: object) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(updates)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _set_mtime(path: Path, iso: str) -> None:
    timestamp = datetime.fromisoformat(iso).timestamp()
    os.utime(path, (timestamp, timestamp))
