from __future__ import annotations

from pathlib import Path


def test_global_index_can_read_latest_owner_and_task_refs(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_indexes import (
        TaskIndexRef,
        latest_owner_refs,
        latest_task_refs,
        register_owner_ref,
        register_task_ref,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(tmp_path, OwnerIdentity.provider_user("feishu", "ou_1"))
    register_owner_ref(home, owner)
    register_task_ref(home, TaskIndexRef(owner_id=owner.owner_id, task_id="task-1", task_path=owner.tasks_dir / "task-1", status="running", title="分析项目"))

    owners = latest_owner_refs(home)
    tasks = latest_task_refs(home, owner_id=owner.owner_id)

    assert owners[0]["owner_id"] == "providers/feishu/users/ou_1"
    assert tasks[0]["task_id"] == "task-1"
    assert tasks[0]["title"] == "分析项目"


def test_backup_manifest_records_owner_refs_without_copying_large_files(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_backup import create_home_backup_manifest
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    (home.owner_memory_daily_dir / "2026-05-31.jsonl").write_text("{}\n", encoding="utf-8")

    manifest = create_home_backup_manifest(home, reason="schema migration")

    assert manifest.manifest_path.exists()
    assert manifest.reason == "schema migration"
    assert str(home.owner_memory_dir) in manifest.included_roots
