from __future__ import annotations

from pathlib import Path


def test_global_index_can_read_latest_owner_and_task_refs(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_indexes import (
        AgentIndexRef,
        RunIndexRef,
        TaskIndexRef,
        dangling_index_refs,
        latest_agent_refs,
        latest_owner_refs,
        latest_run_refs,
        latest_task_refs,
        register_agent_ref,
        register_owner_ref,
        register_run_ref,
        register_task_ref,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(tmp_path, OwnerIdentity.provider_user("feishu", "ou_1"))
    register_owner_ref(home, owner)
    task_path = owner.tasks_dir / "task-1"
    run_path = task_path / "work"
    run_path.mkdir(parents=True)
    register_task_ref(home, TaskIndexRef(owner_id=owner.owner_id, task_id="task-1", task_path=task_path, status="running", title="分析项目"))
    register_run_ref(home, RunIndexRef(owner_id=owner.owner_id, task_id="task-1", run_id="run-1", run_path=run_path, status="running"))
    register_agent_ref(home, AgentIndexRef(owner_id=owner.owner_id, task_id="task-1", agent_id="agent-1", run_path=run_path, status="running"))

    owners = latest_owner_refs(home)
    tasks = latest_task_refs(home, owner_id=owner.owner_id)
    runs = latest_run_refs(home, owner_id=owner.owner_id, task_id="task-1")
    agents = latest_agent_refs(home, owner_id=owner.owner_id, task_id="task-1")

    assert owners[0]["owner_id"] == "providers/feishu/users/ou_1"
    assert tasks[0]["task_id"] == "task-1"
    assert tasks[0]["title"] == "分析项目"
    assert runs[0]["run_id"] == "run-1"
    assert agents[0]["agent_id"] == "agent-1"
    assert dangling_index_refs(home) == []


def test_backup_manifest_records_owner_refs_without_copying_large_files(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_backup import create_home_backup_manifest
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    (home.owner_memory_daily_dir / "2026-05-31.jsonl").write_text("{}\n", encoding="utf-8")

    manifest = create_home_backup_manifest(home, reason="schema change")

    assert manifest.manifest_path.exists()
    assert manifest.reason == "schema change"
    assert str(home.owner_memory_dir) in manifest.included_roots


def test_home_backup_snapshot_can_restore_owner_files(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_backup import (
        create_home_backup_snapshot,
        restore_home_backup_snapshot,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / "home")
    memory_file = home.owner_memory_long_term_dir / "memory.jsonl"
    task_state = home.owner_tasks_dir / "2026-06-01" / "demo" / "work" / "state.json"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    task_state.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text('{"content":"hello"}\n', encoding="utf-8")
    task_state.write_text('{"task_id":"demo"}\n', encoding="utf-8")

    snapshot = create_home_backup_snapshot(home, reason="restore smoke")
    memory_file.unlink()
    task_state.unlink()

    restored = restore_home_backup_snapshot(home, snapshot.backup_dir)

    assert restored.restored_count >= 2
    assert memory_file.read_text(encoding="utf-8") == '{"content":"hello"}\n'
    assert task_state.read_text(encoding="utf-8") == '{"task_id":"demo"}\n'


def test_home_backup_restore_dry_run_reports_paths_without_copying(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_backup import (
        create_home_backup_snapshot,
        plan_home_backup_restore,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / "home")
    memory_file = home.owner_memory_long_term_dir / "memory.jsonl"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text('{"content":"before"}\n', encoding="utf-8")

    snapshot = create_home_backup_snapshot(home, reason="dry-run")
    memory_file.write_text('{"content":"after"}\n', encoding="utf-8")

    plan = plan_home_backup_restore(home, snapshot.backup_dir)

    assert plan.restore_count >= 1
    assert str(memory_file) in plan.restore_paths
    assert memory_file.read_text(encoding="utf-8") == '{"content":"after"}\n'


def test_home_backup_snapshot_report_preserves_bad_manifest(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_backup import (
        create_home_backup_snapshot,
        latest_home_backup_snapshots_report,
    )
    from agent_py_agent.agent.user_space.home_doctor import build_home_doctor_report
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / "home")
    good = create_home_backup_snapshot(home, reason="good")
    bad_manifest = home.system_backups_dir / "backup_20260601T000000Z" / "manifest.json"
    bad_manifest.parent.mkdir(parents=True, exist_ok=True)
    bad_manifest.write_text("{bad-json}\n", encoding="utf-8")

    report = latest_home_backup_snapshots_report(home)
    doctor = build_home_doctor_report(home)

    assert report.snapshots[0]["manifest_path"] == str(good.manifest_path)
    assert report.load_errors
    assert report.load_errors[0]["context"] == "home_backup.manifest"
    assert report.load_errors[0]["path"] == str(bad_manifest)
    assert doctor["backup"]["load_errors"] == report.load_errors
