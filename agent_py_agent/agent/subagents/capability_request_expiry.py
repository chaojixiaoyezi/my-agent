# LLM: 能力申请收口复用原CLOSED及canonical mutate，不授新权限；调用者必须提供宿主确认的父级身份。
# 模块用途: 收起父回合已结束后无人处理的能力申请。
from __future__ import annotations

import time

from .model_capabilities import capability_request_counts_as_open
from .models import FailureType, TaskStatus


# LLM: 显式生命周期入口，不供活动投影读者调用。
# 函数用途: 关闭指定父任务的悬挂能力申请。
def close_stale_capability_requests(store, *, parent_task_id: str, reason: str,
                                    replaced_by_run_id: str = "", run_ids: tuple[str, ...] | None = None) -> list[str]:
    if reason not in {"parent_turn_ended", "parent_turn_interrupted", "replaced", "user_denied"}:
        raise ValueError("invalid capability closeout reason")
    if not parent_task_id:
        raise ValueError("parent task identity is required")
    changed: list[str] = []
    with store.creation_guard():
        replacement = store.load(replaced_by_run_id) if replaced_by_run_id else None
        if run_ids is None:
            report = store.list_runs_for_root_report(parent_task_id)
            if report.load_errors:
                raise OSError("capability closeout candidates unavailable")
            candidates = tuple(task.id for task in report.runs if task.parent_id == parent_task_id)
        else:
            candidates = tuple(dict.fromkeys(run_ids))
        for run_id in candidates:
            _close_waiting_run(store, run_id, parent_task_id, reason, replacement, changed)
    return changed


# LLM: reducer在canonical锁内重新筛OPEN，grant若先赢则不触碰；只关闭已经让出的等待行，不伪装仍运行attempt已经退出。
# 函数用途: 原子提交一条等待申请的CLOSED状态、处置原因与结束展示。
def _close_waiting_run(store, run_id, parent_task_id, reason, replacement, changed):
    def close(current):
        if (current.parent_id != parent_task_id or current.status != TaskStatus.BLOCKED.value
                or current.failure_type != FailureType.CAPABILITY_REQUEST.value or current.runner_active_attempt_id):
            return
        if reason == "replaced" and (replacement is None or not (
                replacement.id != current.id and replacement.parent_id == current.parent_id
                and replacement.root_id == current.root_id and replacement.role == current.role
                and replacement.goal == current.goal and replacement.created_at > current.created_at)):
            return
        pending = [request for request in current.capability_requests if capability_request_counts_as_open(request.status)]
        if not pending:
            return
        resolution = {"reason": reason, "replaced_by_run_id": replacement.id if reason == "replaced" else ""}
        for request in pending:
            request.status = "CLOSED"
            request.constraints = {**dict(request.constraints or {}), "resolution_reason": reason,
                                   "replaced_by_run_id": resolution["replaced_by_run_id"]}
        current.attributes = {**dict(current.attributes or {}), "capability_request_closeout": {
            "resolution": resolution, "request_ids": [request.id for request in pending],
        }}
        current.status = TaskStatus.CANCELLED.value
        current.failure_type = FailureType.CANCELLED.value
        current.blockers = []
        current.current_tool = ""
        current.current_step = "已被替代" if reason == "replaced" else "已拒绝" if reason == "user_denied" else "已过期"
        current.updated_at = current.ended_at = time.time()
        changed.append(current.id)

    store.mutate(run_id, close)
