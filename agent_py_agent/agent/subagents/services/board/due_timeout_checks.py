
from __future__ import annotations

from ...policies import _is_active
from .due_models import DueInspectionContext, DueIssueSpec, _single_issue


def check_heartbeat_timeout_issues(ctx: DueInspectionContext, heartbeat_timeout):
    if not (
        is_runtime_timeout_candidate(ctx.task)
        and heartbeat_timeout > 0
        and ctx.stale_seconds > heartbeat_timeout
    ):
        return []
    severity = "P0" if ctx.stale_seconds > heartbeat_timeout * 3 else "P1"
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                severity,
                "heartbeat_stale",
                f"心跳已停滞 {ctx.stale_seconds:.0f}s，超过配置阈值 {heartbeat_timeout}s。",
                "check_runtime_or_takeover",
            ),
        )
    ]


def check_coordinator_heartbeat_issues(ctx: DueInspectionContext, heartbeat_timeout):
    if not (
        is_parked_planning_coordinator(ctx.task)
        and heartbeat_timeout > 0
        and ctx.stale_seconds > heartbeat_timeout
    ):
        return []
    severity = "P1" if ctx.stale_seconds <= heartbeat_timeout * 3 else "P0"
    child_count = len(getattr(ctx.task, "child_ids", []) or [])
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                severity,
                "coordinator_heartbeat_stale",
                (
                    f"协调节点心跳已停滞 {ctx.stale_seconds:.0f}s，旗下还有 {child_count} 个子任务，"
                    "需要父代理确认是否重新指定 leader。"
                ),
                "recover_coordinator_leadership",
            ),
        )
    ]


def check_run_timeout_issues(ctx: DueInspectionContext, run_timeout):
    if not (is_runtime_timeout_candidate(ctx.task) and run_timeout > 0 and ctx.age_seconds > run_timeout):
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                "run_timeout",
                f"任务已运行 {ctx.age_seconds:.0f}s，超过配置阈值 {run_timeout}s。",
                "shrink_scope_reassign_or_takeover",
            ),
        )
    ]


def is_runtime_timeout_candidate(task) -> bool:
    status = str(task.status or "").upper()
    if is_parked_planning_coordinator(task):
        return False
    return _is_active(status)


def is_parked_planning_coordinator(task) -> bool:
    status = str(getattr(task, "status", "") or "").upper()
    active_attempt = bool(getattr(task, "runner_active_attempt_id", "") or "")
    return status == "PLANNING" and not active_attempt and bool(getattr(task, "child_ids", None))
