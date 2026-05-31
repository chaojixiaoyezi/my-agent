# LLM: Run task workspace indexing is refs-only and must not become a second task truth source.
# 模块用途: 把保存型主代理 run 的任务工作区登记到全局轻量索引，正文仍以 owner task workspace 为准。

from __future__ import annotations

from ..user_space.home_indexes import TaskIndexRef, register_task_ref


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
    except OSError:
        return


__all__ = ["register_saved_run_task_ref"]
