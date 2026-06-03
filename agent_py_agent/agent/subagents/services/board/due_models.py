
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...policies import MakeDueIssueParams, _make_due_issue


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
