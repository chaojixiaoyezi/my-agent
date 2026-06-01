# LLM: Task workspace root resolution keeps machine ids and child runs inside the active task home.
# 模块用途: 从已有 task workspace、父任务传入的 run_workspace 或默认 tasks/<id> 推导任务根目录。

from __future__ import annotations

from pathlib import Path
from typing import Any


# LLM: resolve_task_workspace_root preserves an explicit task root before falling back to local layout.
# 函数用途: 解析任务级 root；子代理继承父任务 root 时不会再开一个独立任务目录。
def resolve_task_workspace_root(workspace: str | Path, task: Any, task_id: str) -> Path:
    task_root = _task_root_from_attrs(getattr(task, "attributes", {}) or {})
    if task_root:
        return Path(task_root)
    existing = str(getattr(task, "task_workspace_dir", "") or "").strip()
    if existing and Path(existing).name == _safe_segment(task_id):
        return Path(existing)
    return Path(workspace) / "tasks" / _safe_segment(task_id)


# LLM: _task_root_from_attrs reads only the structured run_workspace task_root field.
# 函数用途: 避免从自然语言或旧路径猜测任务根目录。
def _task_root_from_attrs(attrs: Any) -> str:
    if not isinstance(attrs, dict):
        return ""
    run_workspace = attrs.get("run_workspace")
    if not isinstance(run_workspace, dict):
        return ""
    return str(run_workspace.get("task_root") or "").strip()


# LLM: _safe_segment only sanitizes directory segments; it does not infer task meaning.
# 函数用途: 把 task/run 标识转成安全目录名，避免斜杠穿透目录层级。
def _safe_segment(value: str) -> str:
    cleaned = str(value or "task").replace("/", "_").replace("\\", "_").strip()
    return cleaned or "task"
