# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""rendering helpers for filesystem task workspace files.

Human version:
These helpers keep task workspace orchestration focused on sync order while this
file owns the small Markdown/YAML bodies written by the runtime memory adapter.
"""

from pathlib import Path
from typing import Any


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 write_task_yaml_if_missing 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write task yaml if missing 相关记录，集中处理目标路径、格式化和状态更新。
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
        "source: subagent_persistence_adapter\n"
        "objective: |-\n"
        f"{_indent_block(goal or '待填写')}\n"
        "legacy:\n"
        f'  task_dir: "{_yaml_quote(str(getattr(task, "task_dir", "")))}"\n'
    )
    path.write_text(content, encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 write_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write summary 相关记录，集中处理目标路径、格式化和状态更新。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 blackboard_content 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 blackboard content 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _yaml_quote 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 yaml quote 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _yaml_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _indent_block 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 indent block 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _indent_block(value: str) -> str:
    return "\n".join(f"  {line}" for line in value.splitlines() or [""])


__all__ = ["blackboard_content", "write_summary", "write_task_yaml_if_missing"]
