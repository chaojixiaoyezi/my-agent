
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...common.path_segments import safe_path_segment


def resolve_task_workspace_root(workspace: str | Path, task: Any, task_id: str) -> Path:
    task_root = _task_root_from_attrs(getattr(task, "attributes", {}) or {})
    if task_root:
        return Path(task_root)
    existing = str(getattr(task, "task_workspace_dir", "") or "").strip()
    if existing and Path(existing).name == _segment(task_id):
        return Path(existing)
    return Path(workspace) / "tasks" / _segment(task_id)


def _task_root_from_attrs(attrs: Any) -> str:
    if not isinstance(attrs, dict):
        return ""
    run_workspace = attrs.get("run_workspace")
    if not isinstance(run_workspace, dict):
        return ""
    return str(run_workspace.get("task_root") or "").strip()


def _segment(value: str) -> str:
    return safe_path_segment(value, default="task", replacement="_")
