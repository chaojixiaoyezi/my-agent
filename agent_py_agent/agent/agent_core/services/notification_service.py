
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import SimpleAgent


def notify_completed_tasks(agent: SimpleAgent, records: list) -> None:
    if not getattr(agent.config, "notification_enabled", False):
        return

    final_statuses = {"DONE", "FAILED", "TIMEOUT"}
    notified_run_ids: set[str] = set()

    for record in records:
        run_id = record.run_id
        if not _should_consider_notification(record, notified_run_ids, final_statuses):
            continue
        task = _load_final_task(agent, run_id, final_statuses)
        if task is None:
            continue

        notified_run_ids.add(run_id)
        _deliver_task_notification(agent, task, run_id)


def _should_consider_notification(record, notified_run_ids: set[str], final_statuses: set[str]) -> bool:
    # LLM: notification eligibility is separate from delivery side effects.
    return (
        record.step in {"runner", "acceptance"}
        and record.applied
        and record.run_id not in notified_run_ids
        and getattr(record, "after_status", "") in final_statuses
    )


def _load_final_task(agent: SimpleAgent, run_id: str, final_statuses: set[str]):
    try:
        task = agent.subagents.load(run_id)
    except FileNotFoundError:
        return None
    return task if task.status in final_statuses else None


def _deliver_task_notification(agent: SimpleAgent, task, run_id: str) -> None:
    try:
        from ..notification import NotificationManager, NotificationRouter

        notif_manager = NotificationManager(agent.config)
        channel = getattr(task, "last_active_channel", "") or "chat"
        notification = notif_manager.create_notification(
            task_id=run_id,
            user_id=getattr(agent.config, "user_id", "admin"),
            session_id=task.root_id or "",
            channel=channel,
            message=_task_completion_message(task, run_id),
        )
        NotificationRouter(notif_manager, agent.config).deliver(notification.notification_id)
    except Exception:
        pass


def _task_completion_message(task, run_id: str) -> str:
    return (
        f"任务 {run_id} 已完成\n"
        f"状态: {task.status}\n"
        f"目标: {task.goal[:100]}\n"
        f"尝试次数: {task.runner_attempts}"
    )
