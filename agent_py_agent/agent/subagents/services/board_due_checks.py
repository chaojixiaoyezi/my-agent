from __future__ import annotations

"""LLM: due-check issue builders for subagent board service.

给人看的解释：
这里把单任务巡检拆出 board.py，用一个上下文对象承载重复参数，避免每个检查函数都有长参数列表。
"""

from dataclasses import dataclass
from typing import Any

from ..policies import MakeDueIssueParams, _is_active, _make_due_issue


@dataclass(frozen=True)
class DueCheckSettings:
    """Runtime thresholds for one due-check pass."""

    now: float
    heartbeat_timeout: float
    run_timeout: float
    min_evidence: int


@dataclass(frozen=True)
class DueInspectionContext:
    """Shared values used by all due-check predicates for one task."""

    task: Any
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
                "missing_work_order_files",
                f"工单目录缺少 {len(validation.missing)} 个关键路径，后续接管和验收不可靠。",
                "repair_work_order",
            ),
        )
    ]


def _check_status_issues(ctx: DueInspectionContext):
    """Check for failed/timeout/channel error/blocked status issues."""
    task = ctx.task
    if task.status not in {"FAILED", "TIMEOUT", "CHANNEL_ERROR", "BLOCKED"}:
        return []
    severity = {"FAILED": "P0", "TIMEOUT": "P0", "CHANNEL_ERROR": "P0", "BLOCKED": "P1"}[task.status]
    action = {
        "FAILED": "inspect_failure_and_reassign_or_takeover",
        "TIMEOUT": "shrink_scope_or_takeover",
        "CHANNEL_ERROR": "probe_channel_before_reassign",
        "BLOCKED": "classify_blocker_and_route_capability",
    }[task.status]
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                severity,
                f"status_{task.status.lower()}",
                f"任务状态为 {task.status}，需要父代理确认原因，不能当作完成。",
                action,
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
                "channel_broken",
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
                "channel_degraded",
                "最近一次通道检查为 DEGRADED，建议先修复弱项再继续派工。",
                "inspect_channel_probe_evidence",
            ),
        )
    ]


def _check_probe_missing_issues(ctx: DueInspectionContext):
    """Check for missing channel probe evidence."""
    if ctx.task.status != "CHANNEL_ERROR" or ctx.task.last_probe_at:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                "channel_probe_missing",
                "任务状态为 CHANNEL_ERROR，但还没有 probe 证据。",
                "run_channel_probe",
            ),
        )
    ]


def _check_done_evidence_issues(ctx: DueInspectionContext, min_evidence):
    """Check for DONE task with insufficient evidence."""
    task = ctx.task
    if task.status != "DONE" or min_evidence <= 0 or len(task.evidence) >= min_evidence:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P0",
                "fake_done_risk",
                f"DONE 任务只有 {len(task.evidence)} 条证据，少于配置要求的 {min_evidence} 条。",
                "require_evidence_or_reopen",
            ),
        )
    ]


def _check_done_verification_issues(ctx: DueInspectionContext):
    """Check for DONE task without verification."""
    if ctx.task.status != "DONE" or ctx.task.verification_status == "VERIFIED":
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P1",
                "unverified_done",
                "任务已标记 DONE，但 verification_status 还不是 VERIFIED。",
                "run_acceptance_or_assign_reviewer",
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
                "open_capability_request",
                f"存在 {ctx.open_request_count} 条未处理能力请求。",
                "route_capability_request",
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
                "open_capability_gap",
                f"存在 {ctx.open_gap_count} 条未关闭能力缺口。",
                "triage_gap_for_learning_or_tooling",
            ),
        )
    ]


def _check_heartbeat_timeout_issues(ctx: DueInspectionContext, heartbeat_timeout):
    """Check for stale heartbeat on active tasks."""
    if not (_is_active(ctx.task.status) and heartbeat_timeout > 0 and ctx.stale_seconds > heartbeat_timeout):
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


def _check_run_timeout_issues(ctx: DueInspectionContext, run_timeout):
    """Check for run timeout on active tasks."""
    if not (_is_active(ctx.task.status) and run_timeout > 0 and ctx.age_seconds > run_timeout):
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


def inspect_single_task_due(
    manager,
    task,
    settings: DueCheckSettings,
    risk_flags_builder,
):
    """Inspect a single task for due issues. Returns a list of issues."""
    open_request_count = sum(1 for item in task.capability_requests if item.status == "OPEN")
    open_gap_count = sum(1 for item in task.capability_gaps if item.status == "OPEN")
    ctx = DueInspectionContext(
        task=task,
        risk_flags=risk_flags_builder(task, open_request_count, open_gap_count),
        open_request_count=open_request_count,
        open_gap_count=open_gap_count,
        age_seconds=max(0.0, settings.now - (task.created_at or settings.now)),
        stale_seconds=max(0.0, settings.now - (task.heartbeat_at or task.updated_at or settings.now)),
    )
    validation = manager.validate_work_order(task.id)
    issues = []
    issues.extend(_check_work_order_issues(ctx, validation))
    issues.extend(_check_status_issues(ctx))
    issues.extend(_check_channel_broken_issues(ctx))
    issues.extend(_check_channel_degraded_issues(ctx))
    issues.extend(_check_probe_missing_issues(ctx))
    issues.extend(_check_done_evidence_issues(ctx, settings.min_evidence))
    issues.extend(_check_done_verification_issues(ctx))
    issues.extend(_check_capability_request_issues(ctx))
    issues.extend(_check_capability_gap_issues(ctx))
    issues.extend(_check_heartbeat_timeout_issues(ctx, settings.heartbeat_timeout))
    issues.extend(_check_run_timeout_issues(ctx, settings.run_timeout))
    return issues
