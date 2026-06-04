
from __future__ import annotations

"""rendering helpers for filesystem task workspace files.

Human version:
These helpers keep task workspace orchestration focused on sync order while this
file owns the small Markdown/YAML bodies written by the runtime memory adapter.
"""

from pathlib import Path
from typing import Any


def write_task_yaml_if_missing(path: Path, task_id: str, task: Any, now: float) -> None:
    """Write the task workspace identity file once, preserving manual edits."""

    if path.exists():
        return
    goal = str(getattr(task, "goal", ""))
    content = (
        "version: 1\n"
        f'task_id: "{_yaml_quote(task_id)}"\n'
        f'primary_run_id: "{_yaml_quote(str(getattr(task, "id", "")))}"\n'
        f'parent_run_id: "{_yaml_quote(str(getattr(task, "parent_id", "")))}"\n'
        f"depth: {int(getattr(task, 'depth', 0) or 0)}\n"
        f'created_at: {float(getattr(task, "created_at", 0.0) or now)}\n'
        "source: subagent_task_workspace\n"
        "objective: |-\n"
        f"{_indent_block(goal or '待填写')}\n"
    )
    path.write_text(content, encoding="utf-8")


def write_summary(path: Path, task_id: str, run_id: str, task: Any) -> None:
    """Write the current task summary view."""

    latest = str(getattr(task, "latest_summary", "")) or "暂无"
    content = (
        "# Current Summary\n\n"
        f"- task_id: {task_id}\n"
        f"- primary_run_id: {run_id}\n"
        f"- status: {getattr(task, 'status', '')}\n"
        f"- current_step: {getattr(task, 'current_step', '') or getattr(task, 'status', '')}\n\n"
        "## Latest\n\n"
        f"{latest}\n"
    )
    path.write_text(content, encoding="utf-8")


def write_parent_summary_placeholder(path: Path, task_id: str, child_run_id: str) -> None:
    """Create a parent task summary without copying child-only status text."""

    content = (
        "# Current Summary\n\n"
        f"- task_id: {task_id}\n"
        f"- primary_run_id: {task_id}\n"
        "- status: RUNNING\n"
        "- current_step: waiting_for_child_runs\n\n"
        "## Latest\n\n"
        f"子代理 {child_run_id} 已登记，等待父任务汇总。\n"
    )
    path.write_text(content, encoding="utf-8")


def blackboard_content(task_id: str) -> str:
    """Return the default task-local shared blackboard body."""

    return (
        "# Blackboard\n\n"
        f"- task_id: {task_id}\n"
        "- purpose: shared task-local facts, decisions, blockers, and handoff notes\n\n"
        "## Facts\n\n- 暂无\n\n"
        "## Decisions\n\n- 暂无\n\n"
        "## Blockers\n\n- 暂无\n"
    )


def _yaml_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _indent_block(value: str) -> str:
    return "\n".join(f"  {line}" for line in value.splitlines() or [""])


__all__ = [
    "blackboard_content",
    "write_parent_summary_placeholder",
    "write_summary",
    "write_task_yaml_if_missing",
]
