
from __future__ import annotations

from ...task_progress import progress_path, read_task_progress, task_progress_summary
from ..runtime.owner_roots import runtime_owner_root


def attach_task_progress(agent: object, run_id: str, layer: dict[str, object]) -> None:
    root = _progress_root(agent)
    if not root or not run_id:
        return
    path = progress_path(root, run_id)
    if not path.exists():
        return
    progress = read_task_progress(root, run_id)
    summary = task_progress_summary({**progress, "ref": str(path)})
    if summary["summary"] or summary["next_action"] or summary["counts"].get("total", 0):
        layer["task_progress"] = summary


def _progress_root(agent: object):
    try:
        return runtime_owner_root(agent)
    except AttributeError:
        return None


__all__ = ["attach_task_progress"]
