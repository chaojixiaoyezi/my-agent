# LLM: Shared due-check DTOs and issue builders; keep board_due_checks focused on predicates.
# 模块用途: 放置子代理 due-check 巡检的共享参数包和 issue 构造 helper，避免单个巡检文件继续膨胀。

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..policies import MakeDueIssueParams, _make_due_issue


# LLM: DueCheckSettings carries runtime thresholds for one due-check pass.
# 类用途: 保存一次 due-check 使用的当前时间、心跳超时、运行超时和最少证据数量。
@dataclass(frozen=True)
class DueCheckSettings:
    """Runtime thresholds for one due-check pass."""

    now: float
    heartbeat_timeout: float
    run_timeout: float
    min_evidence: int
    # LLM: no_progress_attempt_limit keeps repeated recovery fuse configurable at the due-check boundary.
    no_progress_attempt_limit: int = 4


# LLM: DueInspectionContext bundles repeated per-task values for all due-check predicates.
# 类用途: 保存单个任务巡检时反复使用的任务、风险标记、能力请求数量和时间差。
@dataclass(frozen=True)
class DueInspectionContext:
    """Shared values used by all due-check predicates for one task."""

    task: Any
    # LLM: task_index lets due predicates compare parent/child states without reloading files.
    task_index: dict[str, Any] | None
    risk_flags: list[str]
    open_request_count: int
    open_gap_count: int
    age_seconds: float
    stale_seconds: float


# LLM: DueIssueSpec is a compact template for one due-check issue.
# 类用途: 描述一条 due-check 问题的严重程度、类型、说明和建议动作。
@dataclass(frozen=True)
class DueIssueSpec:
    """One due-check issue template."""

    severity: str
    kind: str
    message: str
    action: str
    # LLM: related_refs become rescue_context_refs for machine-readable recovery hints.
    related_refs: list[str] | None = None


# LLM: InspectTaskDueRequest keeps the due-check entrypoint bundle-first as predicates grow.
# 类用途: 集中保存单任务 due-check 所需 manager、任务、阈值、风险构造器和任务索引。
@dataclass(frozen=True)
class InspectTaskDueRequest:
    """Bundle for inspecting one task for due-check issues."""

    manager: Any
    task: Any
    settings: DueCheckSettings
    risk_flags_builder: Callable[[Any, int, int], list[str]]
    task_index: dict[str, Any] | None = None


# LLM: _issue_params converts local context/spec into the shared report construction bundle.
# 函数用途: 把巡检上下文和 issue 模板转换为报告层需要的 MakeDueIssueParams。
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


# LLM: _single_issue keeps all due-check issue construction on the canonical report helper.
# 函数用途: 根据上下文和模板生成一条 DueCheckIssue，保持字段来源一致。
def _single_issue(ctx: DueInspectionContext, spec: DueIssueSpec):
    return _make_due_issue(params=_issue_params(ctx, spec))
