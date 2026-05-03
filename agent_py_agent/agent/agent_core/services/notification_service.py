"""LLM: notification handling for completed tasks.

给人看的解释：
对达到终态的任务触发通知。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import SimpleAgent


def notify_completed_tasks(agent: SimpleAgent, records: list) -> None:
    """对达到终态的任务触发通知。

    根据 records 中的 step、applied、after_status 字段，
    对已到达终态（DONE/FAILED/TIMEOUT）的任务发送通知。
    """
    if not getattr(agent.config, "notification_enabled", False):
        return

    final_statuses = {"DONE", "FAILED", "TIMEOUT"}
    notified_run_ids: set[str] = set()

    for record in records:
        if record.step not in {"runner", "acceptance"}:
            continue
        if not record.applied:
            continue
        run_id = record.run_id
        if run_id in notified_run_ids:
            continue

        after_status = getattr(record, "after_status", "")
        if after_status not in final_statuses:
            continue

        try:
            task = agent.subagents.load(run_id)
        except FileNotFoundError:
            continue

        if task.status not in final_statuses:
            continue

        notified_run_ids.add(run_id)

        try:
            from ..notification import NotificationManager, NotificationRouter

            notif_manager = NotificationManager(agent.config)
            channel = getattr(task, "last_active_channel", "") or "chat"
            message = (
                f"任务 {run_id} 已完成\n"
                f"状态: {task.status}\n"
                f"目标: {task.goal[:100]}\n"
                f"尝试次数: {task.runner_attempts}"
            )
            notification = notif_manager.create_notification(
                task_id=run_id,
                user_id=getattr(agent.config, "user_id", "admin"),
                session_id=task.root_id or "",
                channel=channel,
                message=message,
            )
            router = NotificationRouter(notif_manager, agent.config)
            router.deliver(notification.notification_id)
        except Exception:
            pass