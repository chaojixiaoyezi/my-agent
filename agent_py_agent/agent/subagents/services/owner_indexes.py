
from __future__ import annotations

from typing import Any

from ...user_space.home_indexes import (
    AgentIndexRef,
    RunIndexRef,
    TaskIndexRef,
    register_agent_ref,
    register_run_ref,
    register_task_ref,
)
from ..models import SubAgentTask


def register_owner_runtime_indexes(manager: Any, task: SubAgentTask) -> None:
    home_paths = getattr(manager, "home_paths", None)
    if home_paths is None:
        return
    owner_id = str(getattr(home_paths, "owner_id", "") or task.owner or getattr(manager, "owner_id", "") or "")
    task_id = str(task.root_id or task.id)
    task_path = str(getattr(task, "task_workspace_dir", "") or "").strip()
    run_path = str(getattr(task, "agent_run_workspace_dir", "") or task.task_dir or "").strip()
    try:
        if task_path:
            register_task_ref(
                home_paths,
                TaskIndexRef(
                    owner_id=owner_id,
                    task_id=task_id,
                    task_path=task_path,
                    status=task.status,
                    title=task.goal,
                ),
            )
        if run_path:
            register_run_ref(
                home_paths,
                RunIndexRef(
                    owner_id=owner_id,
                    run_id=task.id,
                    task_id=task_id,
                    run_path=run_path,
                    status=task.status,
                ),
            )
        register_agent_ref(
            home_paths,
            AgentIndexRef(
                owner_id=owner_id,
                agent_id=task.id,
                task_id=task_id,
                run_path=run_path,
                status=task.status,
            ),
        )
    except OSError:
        return

