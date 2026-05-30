# LLM: Agent tree progress keeps optional work ledgers out of the core tree renderer.
# 模块用途: 给 agent tree 节点补充 task_progress 摘要；只读账本，不调度、不验收。

from __future__ import annotations

from ..task_progress import progress_path, read_task_progress, task_progress_summary


def attach_task_progress(agent: object, run_id: str, layer: dict[str, object]) -> None:
    root = getattr(agent, "root", None)
    if not root or not run_id:
        return
    path = progress_path(root, run_id)
    if not path.exists():
        return
    progress = read_task_progress(root, run_id)
    summary = task_progress_summary({**progress, "ref": str(path)})
    if summary["summary"] or summary["next_action"] or summary["counts"].get("total", 0):
        layer["task_progress"] = summary


__all__ = ["attach_task_progress"]
