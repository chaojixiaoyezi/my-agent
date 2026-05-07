# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: notify_completed_tasks 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理notifycompletedtasks相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
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


# LLM: _should_consider_notification 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 判断consider通知条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _should_consider_notification(record, notified_run_ids: set[str], final_statuses: set[str]) -> bool:
    # LLM: notification eligibility is separate from delivery side effects.
    return (
        record.step in {"runner", "acceptance"}
        and record.applied
        and record.run_id not in notified_run_ids
        and getattr(record, "after_status", "") in final_statuses
    )


# LLM: _load_final_task 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询final任务需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _load_final_task(agent: SimpleAgent, run_id: str, final_statuses: set[str]):
    try:
        task = agent.subagents.load(run_id)
    except FileNotFoundError:
        return None
    return task if task.status in final_statuses else None


# LLM: _deliver_task_notification 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理deliver任务通知相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
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


# LLM: _task_completion_message 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理任务completion消息相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _task_completion_message(task, run_id: str) -> str:
    return (
        f"任务 {run_id} 已完成\n"
        f"状态: {task.status}\n"
        f"目标: {task.goal[:100]}\n"
        f"尝试次数: {task.runner_attempts}"
    )
