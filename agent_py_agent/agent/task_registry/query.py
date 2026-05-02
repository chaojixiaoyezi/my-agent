from __future__ import annotations

"""LLM: task query interface.

给人看的解释：
提供跨会话查询接口，包括任务摘要。
"""

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..local_store import LocalStore


def get_task_summary(store: LocalStore, task_id: str) -> str:
    """获取任务摘要文本。

    Args:
        store: LocalStore 实例
        task_id: 任务 ID

    Returns:
        格式化的任务摘要文本
    """

    task_info = store.task_registry.lookup_task(task_id)

    if not task_info:
        return f"任务 {task_id} 不存在。"

    status_emoji = {
        "PLANNING": "⏳",
        "RUNNING": "🔄",
        "BLOCKED": "⚠️",
        "FAILED": "❌",
        "DONE": "✅",
        "TIMEOUT": "⏰",
        "SPLIT": "🔀",
    }.get(task_info["status"], "❓")

    lines = [
        f"任务 ID: {task_info['task_id']}",
        f"状态: {status_emoji} {task_info['status']}",
        f"目标: {task_info['goal'][:100]}..." if len(task_info['goal']) > 100 else task_info['goal'],
    ]

    # 添加创建和更新时间
    if task_info["created_at"]:
        created_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(task_info["created_at"]))
        lines.append(f"创建时间: {created_time}")

    if task_info["updated_at"] and task_info["updated_at"] != task_info["created_at"]:
        updated_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(task_info["updated_at"]))
        lines.append(f"最后更新: {updated_time}")

    # 添加会话和用户信息
    if task_info["session_id"]:
        lines.append(f"会话 ID: {task_info['session_id']}")

    if task_info["user_id"]:
        lines.append(f"用户 ID: {task_info['user_id']}")

    return "\n".join(lines)


def format_task_list(tasks: list[dict]) -> str:
    """格式化任务列表。

    Args:
        tasks: 任务信息列表

    Returns:
        格式化的任务列表文本
    """

    if not tasks:
        return "没有找到任务。"

    lines = []
    status_emoji = {
        "PLANNING": "⏳",
        "RUNNING": "🔄",
        "BLOCKED": "⚠️",
        "FAILED": "❌",
        "DONE": "✅",
        "TIMEOUT": "⏰",
        "SPLIT": "🔀",
    }

    for task in tasks[:20]:  # 最多显示 20 个
        status_symbol = status_emoji.get(task["status"], "❓")
        goal_preview = task["goal"][:80] + "..." if len(task["goal"]) > 80 else task["goal"]

        lines.append(f"{status_symbol} [{task['task_id']}] {goal_preview}")

    if len(tasks) > 20:
        lines.append(f"... 还有 {len(tasks) - 20} 个任务")

    return "\n".join(lines)
