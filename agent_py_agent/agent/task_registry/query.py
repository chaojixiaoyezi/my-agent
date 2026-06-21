
from __future__ import annotations

"""task query interface.

提供跨会话查询接口，包括任务摘要。
"""

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..local_storage import LocalStore

_TASK_STATUS_EMOJI = {
    "PLANNING": "⏳",
    "RUNNING": "🔄",
    "BLOCKED": "⚠️",
    "PAUSED": "⏸️",
    "ABANDONED": "🗑️",
    "FAILED": "❌",
    "DONE": "✅",
    "TIMEOUT": "⏰",
    "TAKEN_OVER": "🔀",
}


def get_task_summary(store: LocalStore, task_id: str) -> str:

    task_info = store.task_registry.lookup_task(task_id)

    if not task_info:
        return f"任务 {task_id} 不存在。"

    status_emoji = _TASK_STATUS_EMOJI.get(task_info["status"], "❓")

    lines = [
        f"任务 ID: {task_info['task_id']}",
        f"状态: {status_emoji} {task_info['status']}",
    ]

    # 显示 description（存在 goal 字段前 100 字）
    goal = task_info["goal"]
    if goal:
        lines.append(f"描述: {goal[:100]}")

    # 添加创建和更新时间
    if task_info["created_at"]:
        created_time = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(task_info["created_at"]))
        lines.append(f"创建时间: {created_time}")

    if task_info["updated_at"] and task_info["updated_at"] != task_info["created_at"]:
        updated_time = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(task_info["updated_at"]))
        lines.append(f"最后更新: {updated_time}")

    # 添加会话和用户信息
    if task_info["session_id"]:
        lines.append(f"会话 ID: {task_info['session_id']}")

    if task_info["user_id"]:
        lines.append(f"用户 ID: {task_info['user_id']}")

    return "\n".join(lines)


def format_task_list(tasks: list[dict]) -> str:

    if not tasks:
        return "没有找到任务。"

    lines = ["任务列表："]

    for task in tasks[:20]:  # 最多显示 20 个
        status_symbol = _TASK_STATUS_EMOJI.get(task["status"], "❓")
        goal_preview = task["goal"][:80] + "..." if len(task["goal"]) > 80 else task["goal"]
        task_id = task["task_id"]

        lines.append(f"  [{task['status']}] {task_id}  {goal_preview}")

    if len(tasks) > 20:
        lines.append(f"... 还有 {len(tasks) - 20} 个任务")

    return "\n".join(lines)
