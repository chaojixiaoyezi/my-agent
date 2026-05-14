# LLM: Timeout due-check predicates stay isolated from ordinary status/evidence checks.
# 模块用途: 检查子代理 runner 心跳超时、运行超时，以及 coordinator 等待子任务时的失联风险。

from __future__ import annotations

from ..policies import _is_active
from .board_due_models import DueInspectionContext, DueIssueSpec, _single_issue


# LLM: check_heartbeat_timeout_issues reports stale heartbeats for active runner tasks.
# 函数用途: 检查真实执行中的子代理心跳是否超过阈值，生成 takeover/检查建议。
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


# LLM: check_coordinator_heartbeat_issues reports orphan-risk coordinators without treating them as runners.
# 函数用途: 检查有子任务的 coordinator 是否失联；只生成领导权恢复建议，不触发普通 runner timeout。
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


# LLM: check_run_timeout_issues reports active runs that exceed the configured wall-clock budget.
# 函数用途: 检查真实执行中的子代理是否运行过久，生成缩小范围、接管或重派建议。
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


# LLM: is_runtime_timeout_candidate separates real runner stalls from parked coordinator planning nodes.
# 函数用途: 判断任务是否应该进入 heartbeat/run timeout 规则；有子任务的 PLANNING 协调节点不按 runner 卡死处理。
def is_runtime_timeout_candidate(task) -> bool:
    status = str(task.status or "").upper()
    if is_parked_planning_coordinator(task):
        return False
    return _is_active(status)


# LLM: is_parked_planning_coordinator identifies parent-only planning nodes with live child ownership.
# 函数用途: 判断一个任务是否是等待子任务的 coordinator，而不是正在执行的 runner。
def is_parked_planning_coordinator(task) -> bool:
    status = str(getattr(task, "status", "") or "").upper()
    active_attempt = bool(getattr(task, "runner_active_attempt_id", "") or "")
    return status == "PLANNING" and not active_attempt and bool(getattr(task, "child_ids", None))
