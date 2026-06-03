
from __future__ import annotations

from ..user_space.home_indexes import RunIndexRef, TaskIndexRef, register_run_ref, register_task_ref


def register_saved_run_task_ref(agent, result, params) -> None:
    home_paths = getattr(agent, "home_paths", None)
    if home_paths is None:
        return
    task_id = params.task_id or params.run_id or params.run_request_id
    if not task_id:
        return
    try:
        register_task_ref(
            home_paths,
            TaskIndexRef(
                owner_id=str(getattr(home_paths, "owner_id", "") or ""),
                task_id=task_id,
                task_path=result.root,
                status="active",
                title=params.user_prompt[:160],
            ),
        )
        register_run_ref(
            home_paths,
            RunIndexRef(
                owner_id=str(getattr(home_paths, "owner_id", "") or ""),
                run_id=str(params.run_id or params.run_request_id or task_id),
                task_id=str(task_id),
                run_path=result.work_dir,
                status="active",
            ),
        )
    except OSError:
        return


__all__ = ["register_saved_run_task_ref"]
