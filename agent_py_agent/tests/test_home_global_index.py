from __future__ import annotations

from pathlib import Path


# LLM: global indexes are rebuildable maps, not authoritative task state.
# 函数用途: 验证 owner/task 轻量索引只写引用，真实状态仍在 owner/task 目录。
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
