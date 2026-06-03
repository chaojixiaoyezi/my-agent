

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...runtime_errors import runtime_error_report
from ...settings.defaults import default_config_bool

if TYPE_CHECKING:
    from ..core import SimpleAgent

_DEFAULT_NOTIFICATION_ENABLED = default_config_bool("notification_enabled")
_LOGGER = logging.getLogger(__name__)


def notify_completed_tasks(agent: SimpleAgent, records: list) -> None:
    if not getattr(agent.config, "notification_enabled", _DEFAULT_NOTIFICATION_ENABLED):
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
        from ...notification import NotificationManager, NotificationRouter

        notif_manager = NotificationManager(agent.config)
        channel = getattr(task, "last_active_channel", "") or "chat"
        notification = notif_manager.create_notification(
            task_id=run_id,
            user_id=getattr(agent.config, "user_id", "admin"),
            session_id=task.root_id or "",
            channel=channel,
            message=_task_completion_message(task, run_id),
        )
        try:
            NotificationRouter(agent.config).deliver(notification.notification_id)
        except Exception as exc:
            _mark_notification_failed(notif_manager, notification.notification_id, exc)
    except Exception as exc:
        _warn_notification_failure(exc, context="notification.create_or_deliver", run_id=run_id)


def _mark_notification_failed(notif_manager, notification_id: str, exc: Exception) -> None:
    try:
        marked = notif_manager.mark_failed(notification_id, _notification_error_message(exc))
    except Exception as mark_exc:
        _warn_notification_failure(
            mark_exc,
            context="notification.mark_failed",
            notification_id=notification_id,
        )
        return
    if not marked:
        _LOGGER.warning(
            "notification failure could not be recorded: %s",
            {
                "notification_id": notification_id,
                "delivery_error": _notification_error_message(exc),
                "context": "notification.mark_failed_missing_record",
            },
        )


def _task_completion_message(task, run_id: str) -> str:
    return (
        f"任务 {run_id} 已完成\n"
        f"状态: {task.status}\n"
        f"目标: {task.goal[:100]}\n"
        f"尝试次数: {task.runner_attempts}"
    )


def _notification_error_message(exc: Exception) -> str:
    message = f"{exc.__class__.__name__}: {exc}"
    return message[:500]


def _warn_notification_failure(
    exc: Exception,
    *,
    context: str,
    run_id: str = "",
    notification_id: str = "",
) -> None:
    report = runtime_error_report(exc, context=context)
    if run_id:
        report["run_id"] = run_id
    if notification_id:
        report["notification_id"] = notification_id
    _LOGGER.warning("notification runtime failure: %s", report)
