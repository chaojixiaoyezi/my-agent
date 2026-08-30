from __future__ import annotations

from pathlib import Path


def test_global_index_records_owner_and_task_refs(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_indexes import (
        TaskIndexRef,
        register_owner_ref,
        register_task_ref,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(home.root, OwnerIdentity.provider_user("feishu", "ou_1"))

    owner_record = register_owner_ref(home, owner)
    task_record = register_task_ref(
        home,
        TaskIndexRef(
            owner_id=owner.owner_id,
            task_id="task_1",
            task_path=owner.tasks_dir / "task_1",
            status="running",
            title="测试任务",
        ),
    )

    assert owner_record["owner_home"] == str(owner.home_dir)
    assert task_record["owner_id"] == "providers/feishu/users/ou_1"
    assert home.global_index_owners_jsonl.exists()
    assert home.global_index_active_tasks_jsonl.exists()


def test_global_index_skips_unchanged_projection_rows(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.home_indexes import (
        TaskIndexRef,
        register_task_ref,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    ref = TaskIndexRef(
        owner_id=home.owner_id,
        task_id="task-stable",
        task_path=home.owner_tasks_dir / "task-stable",
        status="running",
        title="稳定任务",
    )

    register_task_ref(home, ref)
    register_task_ref(home, ref)
    assert len(home.global_index_active_tasks_jsonl.read_text(encoding="utf-8").splitlines()) == 1

    register_task_ref(home, TaskIndexRef(**{**ref.__dict__, "status": "done"}))
    assert len(home.global_index_active_tasks_jsonl.read_text(encoding="utf-8").splitlines()) == 2


def test_global_index_latest_refs_ignore_stale_duplicate_paths(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_indexes import (
        TaskIndexRef,
        dangling_index_refs,
        latest_task_refs,
        register_task_ref,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(home.root, OwnerIdentity.provider_user("feishu", "ou_2"))
    missing_old = owner.tasks_dir / "missing-old"
    current = owner.tasks_dir / "task_1"
    current.mkdir(parents=True)

    register_task_ref(home, TaskIndexRef(owner_id=owner.owner_id, task_id="task_1", task_path=missing_old, status="running", title="旧标题"))
    register_task_ref(home, TaskIndexRef(owner_id=owner.owner_id, task_id="task_1", task_path=current, status="done", title="新标题"))

    tasks = latest_task_refs(home, owner_id=owner.owner_id)

    assert len(tasks) == 1
    assert tasks[0]["status"] == "done"
    assert tasks[0]["task_path"] == str(current)
    assert dangling_index_refs(home) == []


def test_global_index_latest_task_refs_report_corrupt_rows(tmp_path: Path):
    from agent_py_agent.agent.user_space.home_indexes import (
        TaskIndexRef,
        latest_task_refs_report,
        register_task_ref,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    home = ensure_my_agent_home(tmp_path)
    owner = ensure_owner_home(home.root, OwnerIdentity.provider_user("feishu", "ou_3"))
    home.global_index_active_tasks_jsonl.write_text("{bad-json}\n", encoding="utf-8")
    task_path = owner.tasks_dir / "task_1"
    task_path.mkdir(parents=True)
    register_task_ref(home, TaskIndexRef(owner_id=owner.owner_id, task_id="task_1", task_path=task_path, status="running", title="新标题"))

    report = latest_task_refs_report(home, owner_id=owner.owner_id)

    assert [row["task_id"] for row in report.records] == ["task_1"]
    assert report.load_errors
    assert report.load_errors[0]["context"] == "home_indexes.active_tasks"
