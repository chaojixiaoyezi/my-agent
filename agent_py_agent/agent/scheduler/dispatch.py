# LLM: 派发仍使用原 Repository、SessionTask 和 Wake；事件与时间只是同一原运行的不同触发器，不新增队列或执行循环。
#   cheap_checks仅透传给到期/队列只读候选读取，取消、重启恢复、事件预留与通知仍逐拍现算。
# 模块用途: 组合原 scheduler 派发步骤并在唤醒前收口退休事件，避免服务类增长为第二执行器。
from __future__ import annotations

import time

from .repository import SchedulerNotFoundError


# LLM: 原服务唯一入口委托到此；经queued_runs(exclude_active_lanes=True)按同job/rule车道筛选候选，job级准入在prepare/wake前原子提交。
# 函数用途: 把已就绪且无同车道冲突的原运行接入同一 Wake，并对已退代事件留下取消终态。
def enqueue_ready_runs(service, *, now=None, limit=32, cheap_checks=False):
    from .policy import queue_expired
    from .service import _SCHEDULER_WAKE_REASON, _wake_metadata

    current = float(time.time() if now is None else now)
    if service.cancellation_handler is not None:
        for run in service.repository.pending_cancellation_runs():
            service.cancellation_handler(run)
    service.repository.recover_interrupted_executions(now=current)
    service.repository.reserve_due_runs(now=current, limit=limit, cheap_checks=cheap_checks)
    service.repository.reserve_event_runs(now=current, limit=limit)
    wake_ids: list[str] = []
    for run in service.repository.queued_runs(
        now=current,
        limit=max(1, limit * 2),
        cheap_checks=cheap_checks,
        exclude_active_lanes=True,
    ):
        if not service.repository.event_run_is_current(run):
            service._fail_without_execution(run, 'EVENT_SOURCE_RETIRED', now=current, status='cancelled')
            continue
        if queue_expired(run, current):
            service._fail_without_execution(run, "SCHEDULER_QUEUE_EXPIRED", now=current, status="skipped")
            continue
        skill_error = service._skill_reference_error(run)
        if skill_error:
            service._fail_without_execution(run, skill_error, now=current)
            continue
        try:
            run = service.repository.admit_run_dispatch(str(run["run_id"]), now=current)
            if run is None:
                continue
            from .execution import prepare_execution

            run = prepare_execution(service, run, now=current)
            signal = service.conversation_store.wakes.raise_signal(
                {
                    "thread_id": str(run["thread_id"]),
                    # 定时执行本身就是这次后台工作的唯一根任务。只把 run_id
                    # 放在 metadata 会让 BackgroundRunRequest 变成 taskless，
                    # 于是模型刚派完子代理时调度器读不到仍为 active 的任务链接，
                    # 误把第一片模型回复结算成 done。
                    "root_task_id": str(run["run_id"]),
                    "urgency": "normal",
                    "reason": _SCHEDULER_WAKE_REASON,
                    "summary": f"Scheduled job is ready: {run['name']}",
                    "dedupe_key": f"scheduler-run:{run['run_id']}",
                    "metadata": _wake_metadata(run),
                    "now": current,
                }
            )
            wake_id = str(getattr(signal, "wake_signal_id", "") or "")
            service.repository.attach_wake_signal(str(run["run_id"]), wake_id)
            if wake_id:
                wake_ids.append(wake_id)
        except (KeyError, SchedulerNotFoundError):
            # The conversation or run was removed concurrently. A missing
            # thread cannot be guessed or recreated by the scheduler.
            service._fail_without_execution(run, "SCHEDULER_THREAD_UNAVAILABLE", now=current)
        except ValueError:
            if run.get('event_source') is None:
                raise
            service._fail_without_execution(run, 'EVENT_SOURCE_RETIRED', now=current, status='cancelled')
    return wake_ids
