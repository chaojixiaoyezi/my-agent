
from __future__ import annotations

"""due-check issue builders for subagent board service.

这里把单任务巡检拆出 board.py，用一个上下文对象承载重复参数，避免每个检查函数都有长参数列表。
状态检查只走当前 TaskStatus/VerificationStatus helper，未知原文只保留为审计数据。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...model_capabilities import capability_request_counts_as_open
from ...models import (
    SUBAGENT_FAILURE_STATUSES,
    TaskStatus,
    normalize_task_status,
    task_has_status,
    task_is_done_verified,
    task_is_handled_after_parent_timeout,
    task_status_in,
    task_status_reason_code,
)
from ...policies import (
    ACTION_CLASSIFY_BLOCKER,
    ACTION_INSPECT_CHANNEL_PROBE,
    ACTION_RECOVER_CHILD_AFTER_PARENT_TIMEOUT,
    ACTION_RECOVER_COORDINATOR_LEADERSHIP,
    ACTION_REPAIR_WORK_ORDER,
    ACTION_ROUTE_CAPABILITY_REQUEST,
    ACTION_STOP_NO_PROGRESS_AND_ESCALATE,
    ACTION_TRIAGE_CAPABILITY_GAP,
    ISSUE_CHANNEL_BROKEN,
    ISSUE_CHANNEL_DEGRADED,
    ISSUE_CHANNEL_PROBE_MISSING,
    ISSUE_COORDINATOR_HEARTBEAT_STALE,
    ISSUE_COORDINATOR_NEEDS_LEADERSHIP_RECOVERY,
    ISSUE_FAKE_DONE_RISK,
    ISSUE_HEARTBEAT_STALE,
    ISSUE_MISSING_WORK_ORDER_FILES,
    ISSUE_NO_PROGRESS_FUSE,
    ISSUE_OPEN_CAPABILITY_GAP,
    ISSUE_OPEN_CAPABILITY_REQUEST,
    ISSUE_PARENT_TIMEOUT_WITH_UNFINISHED_CHILDREN,
    ISSUE_RUN_TIMEOUT,
    ISSUE_UNVERIFIED_DONE,
    MakeDueIssueParams,
    _is_active,
    _make_due_issue,
)
from ..recovery.strategy import SubagentRecoveryStrategyRequest, build_subagent_recovery_strategy


@dataclass(frozen=True)
class DueCheckSettings:
    """Runtime thresholds for one due-check pass."""

    now: float
    heartbeat_timeout: float
    run_timeout: float
    min_evidence: int
    no_progress_attempt_limit: int = 4


@dataclass(frozen=True)
class DueInspectionContext:
    """Shared values used by all due-check predicates for one task."""

    task: Any
    task_index: dict[str, Any] | None
    risk_flags: list[str]
    open_request_count: int
    open_gap_count: int
    age_seconds: float
    stale_seconds: float


@dataclass(frozen=True)
class DueIssueSpec:
    """One due-check issue template."""

    severity: str
    kind: str
    message: str
    action: str
    related_refs: list[str] | None = None


@dataclass(frozen=True)
class InspectTaskDueRequest:
    """Bundle for inspecting one task for due-check issues."""

    manager: Any
    task: Any
    settings: DueCheckSettings
    risk_flags_builder: Callable[[Any, int, int], list[str]]
    task_index: dict[str, Any] | None = None


def _issue_params(ctx: DueInspectionContext, spec: DueIssueSpec):
    return MakeDueIssueParams(
        task=ctx.task,
        severity=spec.severity,
        kind=spec.kind,
        message=spec.message,
        suggested_action=spec.action,
        risk_flags=ctx.risk_flags,
        open_request_count=ctx.open_request_count,
        open_gap_count=ctx.open_gap_count,
        age_seconds=ctx.age_seconds,
        stale_seconds=ctx.stale_seconds,
        related_refs=spec.related_refs,
    )


def _single_issue(ctx: DueInspectionContext, spec: DueIssueSpec):
    return _make_due_issue(params=_issue_params(ctx, spec))


def _check_work_order_issues(ctx: DueInspectionContext, validation):
    """Check for missing work order files."""
    if validation.ok:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                ISSUE_MISSING_WORK_ORDER_FILES,
                f"工单目录缺少 {len(validation.missing)} 个关键路径，后续接管和验收不可靠。",
                ACTION_REPAIR_WORK_ORDER,
            ),
        )
    ]


def _check_status_issues(ctx: DueInspectionContext):
    """Check for failed/timeout/channel error/blocked status issues."""
    task = ctx.task
    if not task_status_in(task.status, SUBAGENT_FAILURE_STATUSES):
        return []
    status = normalize_task_status(task.status)
    severity = {"FAILED": "P0", "TIMEOUT": "P0", "CHANNEL_ERROR": "P0", "BLOCKED": "P1"}[status]
    action = {
        "FAILED": "inspect_failure_and_reassign_or_takeover",
        "TIMEOUT": "shrink_scope_or_takeover",
        "CHANNEL_ERROR": "probe_channel_before_reassign",
        "BLOCKED": "classify_blocker_and_route_capability",
    }[status]
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                severity,
                f"status_{task_status_reason_code(status)}",
                f"任务状态为 {status}，需要父代理确认原因，不能当作完成。",
                action,
            ),
        )
    ]


def _check_no_progress_fuse_issues(ctx: DueInspectionContext, attempt_limit: int):
    """Check whether repeated recovery attempts must stop automatic retry."""
    if attempt_limit <= 0:
        return []
    strategy = build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(
            task=ctx.task,
            no_progress_attempt_limit=attempt_limit,
        )
    )
    if not strategy.no_progress_fuse:
        return []
    refs = [*strategy.recovery_refs, *strategy.takeover_refs]
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                ISSUE_NO_PROGRESS_FUSE,
                (
                    f"任务已连续尝试 {ctx.task.runner_attempts} 次，达到无进展熔断阈值 {attempt_limit}；"
                    "停止自动重试和扩容，改为汇总 refs 后等待父级/用户决策。"
                ),
                ACTION_STOP_NO_PROGRESS_AND_ESCALATE,
                related_refs=list(dict.fromkeys(ref for ref in refs if ref)),
            ),
        )
    ]


def _check_leadership_recovery_issues(ctx: DueInspectionContext, attempt_limit: int):
    """Check whether a dead coordinator should hand off its child subtree."""
    strategy = build_subagent_recovery_strategy(
        SubagentRecoveryStrategyRequest(
            task=ctx.task,
            no_progress_attempt_limit=attempt_limit,
        )
    )
    if not strategy.leadership_recovery:
        return []
    child_refs = [f"child_run:{run_id}" for run_id in strategy.child_run_ids]
    refs = [*child_refs, *strategy.recovery_refs, *strategy.takeover_refs]
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                ISSUE_COORDINATOR_NEEDS_LEADERSHIP_RECOVERY,
                (
                    f"协调节点状态为 {ctx.task.status}，旗下仍有 {len(strategy.child_run_ids)} 个子任务；"
                    "必须先把子树交给新 leader，不能按普通 worker 新建 takeover run。"
                ),
                ACTION_RECOVER_COORDINATOR_LEADERSHIP,
                related_refs=list(dict.fromkeys(ref for ref in refs if ref)),
            ),
        )
    ]


def _check_channel_broken_issues(ctx: DueInspectionContext):
    """Check for channel broken issues."""
    if ctx.task.channel_status != "BROKEN":
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                ISSUE_CHANNEL_BROKEN,
                "最近一次通道检查为 BROKEN，优先修复 runtime / workdir / JSON 现场。",
                "run_channel_probe_and_fix_runtime",
            ),
        )
    ]


def _check_channel_degraded_issues(ctx: DueInspectionContext):
    """Check for channel degraded issues."""
    if ctx.task.channel_status != "DEGRADED":
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P1",
                ISSUE_CHANNEL_DEGRADED,
                "最近一次通道检查为 DEGRADED，建议先修复弱项再继续派工。",
                ACTION_INSPECT_CHANNEL_PROBE,
            ),
        )
    ]


def _check_probe_missing_issues(ctx: DueInspectionContext):
    """Check for missing channel probe evidence."""
    # Due-check state transitions read the current protocol helpers only.
    if not task_has_status(ctx.task, TaskStatus.CHANNEL_ERROR) or ctx.task.last_probe_at:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                ISSUE_CHANNEL_PROBE_MISSING,
                "任务状态为 CHANNEL_ERROR，但还没有 probe 证据。",
                "run_channel_probe",
            ),
        )
    ]


def _check_done_evidence_issues(ctx: DueInspectionContext, min_evidence):
    """Check for DONE task with insufficient evidence."""
    task = ctx.task
    if not task_has_status(task, TaskStatus.DONE) or min_evidence <= 0 or len(task.evidence) >= min_evidence:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                ISSUE_FAKE_DONE_RISK,
                f"DONE 任务只有 {len(task.evidence)} 条证据，少于配置要求的 {min_evidence} 条。",
                "require_evidence_or_reopen",
            ),
        )
    ]


def _check_done_verification_issues(ctx: DueInspectionContext):
    """Check for DONE task without verification."""
    if not task_has_status(ctx.task, TaskStatus.DONE) or task_is_done_verified(ctx.task):
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P1",
                ISSUE_UNVERIFIED_DONE,
                "任务已标记 DONE，但 verification_status 还不是 VERIFIED。",
                "reopen_for_evidence_or_assign_reviewer",
            ),
        )
    ]


def _check_capability_request_issues(ctx: DueInspectionContext):
    """Check for open capability request issues."""
    if not ctx.open_request_count:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P1",
                ISSUE_OPEN_CAPABILITY_REQUEST,
                f"存在 {ctx.open_request_count} 条未处理能力请求。",
                ACTION_ROUTE_CAPABILITY_REQUEST,
            ),
        )
    ]


def _check_capability_gap_issues(ctx: DueInspectionContext):
    """Check for open capability gap issues."""
    if not ctx.open_gap_count:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P2",
                ISSUE_OPEN_CAPABILITY_GAP,
                f"存在 {ctx.open_gap_count} 条未关闭能力缺口。",
                ACTION_TRIAGE_CAPABILITY_GAP,
            ),
        )
    ]


def check_parent_timeout_child_issues(ctx: DueInspectionContext):
    """Check for unfinished children after a parent has timed out."""
    task = ctx.task
    if not task_has_status(task, TaskStatus.TIMEOUT) or not getattr(task, "child_ids", None):
        return []
    unfinished = _unfinished_child_refs(ctx)
    if not unfinished:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P1",
                ISSUE_PARENT_TIMEOUT_WITH_UNFINISHED_CHILDREN,
                (
                    "父节点已经 TIMEOUT，但仍有未完成子任务需要重新指定领导者或验收入口："
                    f"{_child_ref_summary(unfinished)}。"
                ),
                ACTION_RECOVER_CHILD_AFTER_PARENT_TIMEOUT,
                related_refs=[f"unfinished_child:{ref}" for ref in unfinished],
            ),
        )
    ]


def _unfinished_child_refs(ctx: DueInspectionContext) -> list[str]:
    task_index = ctx.task_index or {}
    refs: list[str] = []
    for child_id in getattr(ctx.task, "child_ids", []) or []:
        child = task_index.get(child_id)
        if child is None:
            refs.append(f"{child_id}:MISSING")
            continue
        if _child_needs_parent_recovery(child):
            refs.append(f"{child.id}:{str(child.status or '').upper()}")
    return refs


def _child_needs_parent_recovery(child) -> bool:
    return not task_is_handled_after_parent_timeout(child)


def _child_ref_summary(refs: list[str]) -> str:
    head = refs[:5]
    suffix = "" if len(refs) <= 5 else f" 等 {len(refs)} 个"
    return ", ".join(head) + suffix


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
                ISSUE_HEARTBEAT_STALE,
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
                ISSUE_COORDINATOR_HEARTBEAT_STALE,
                (
                    f"协调节点心跳已停滞 {ctx.stale_seconds:.0f}s，旗下还有 {child_count} 个子任务，"
                    "需要父代理确认是否重新指定 leader。"
                ),
                ACTION_RECOVER_COORDINATOR_LEADERSHIP,
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
                ISSUE_RUN_TIMEOUT,
                f"任务已运行 {ctx.age_seconds:.0f}s，超过配置阈值 {run_timeout}s。",
                "shrink_scope_reassign_or_takeover",
            ),
        )
    ]


def is_runtime_timeout_candidate(task) -> bool:
    status = str(task.status or "").strip()
    if is_parked_planning_coordinator(task):
        return False
    return _is_active(status)


def is_parked_planning_coordinator(task) -> bool:
    active_attempt = bool(getattr(task, "runner_active_attempt_id", "") or "")
    return task_has_status(task, TaskStatus.PLANNING) and not active_attempt and bool(getattr(task, "child_ids", None))


def inspect_single_task_due(request: InspectTaskDueRequest):
    """Inspect a single task for due issues. Returns a list of issues."""
    task = request.task
    open_request_count = sum(
        1 for item in task.capability_requests if capability_request_counts_as_open(getattr(item, "status", "OPEN"))
    )
    open_gap_count = sum(1 for item in task.capability_gaps if item.status == "OPEN")
    ctx = DueInspectionContext(
        task=task,
        task_index=request.task_index,
        risk_flags=request.risk_flags_builder(task, open_request_count, open_gap_count),
        open_request_count=open_request_count,
        open_gap_count=open_gap_count,
        age_seconds=max(0.0, request.settings.now - (task.created_at or request.settings.now)),
        stale_seconds=max(0.0, request.settings.now - (task.heartbeat_at or task.updated_at or request.settings.now)),
    )
    validation = request.manager.validate_work_order(task.id)
    issues = []
    issues.extend(_check_work_order_issues(ctx, validation))
    no_progress_issues = _check_no_progress_fuse_issues(ctx, request.settings.no_progress_attempt_limit)
    leadership_issues = (
        []
        if no_progress_issues
        else _check_leadership_recovery_issues(ctx, request.settings.no_progress_attempt_limit)
    )
    issues.extend(no_progress_issues or leadership_issues or _check_status_issues(ctx))
    issues.extend(check_parent_timeout_child_issues(ctx))
    issues.extend(_check_channel_broken_issues(ctx))
    issues.extend(_check_channel_degraded_issues(ctx))
    issues.extend(_check_probe_missing_issues(ctx))
    issues.extend(_check_done_evidence_issues(ctx, request.settings.min_evidence))
    issues.extend(_check_done_verification_issues(ctx))
    issues.extend(_check_capability_request_issues(ctx))
    issues.extend(_check_capability_gap_issues(ctx))
    issues.extend(check_coordinator_heartbeat_issues(ctx, request.settings.heartbeat_timeout))
    issues.extend(check_heartbeat_timeout_issues(ctx, request.settings.heartbeat_timeout))
    issues.extend(check_run_timeout_issues(ctx, request.settings.run_timeout))
    return issues
