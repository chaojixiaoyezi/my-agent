
"""Planning logic for log-analysis subagent work orders.

Dataclasses live in models.py so this module can stay focused on planning flow.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ...agents.contracts import (
    DEFAULT_ACCEPTANCE_CHECKS,
    DEFAULT_ANALYST_TOOLS,
    normalize_evidence_refs,
)
from ...agents.summaries import render_case_summary, summarize_case
from .models import (
    CreateWorkOrdersParams,
    LogAnalysisWorkOrderPlan,
    PlanInputs,
    SubagentWorkOrder,
)

DEFAULT_REVIEWER_TOOLS = ["evidence_read"]

NO_EVIDENCE_ISSUE = "case has no evidence_refs; analyst/reviewer work orders are not ready"
PLAN_NOT_READY_ISSUE = "work-order plan is not ready; refusing to create subagent tasks"


@dataclass(frozen=True)
class PlanWorkOrdersOptions:
    evidence_refs: Any = None
    route_summary: Mapping[str, Any] | None = None
    quality_contract: Mapping[str, Any] | None = None
    mode: str = "manual"
    dry_run: bool = True


def _get(source: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a mapping or an object."""
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _case_id(case: Any, summary: Mapping[str, Any]) -> str:
    """Return a stable case id from case fields or summary fallback."""
    summary_case = summary.get("case", {})
    if not isinstance(summary_case, Mapping):
        summary_case = {}
    return str(_get(case, "case_id") or _get(case, "id") or summary_case.get("case_id") or "unknown-case")


def _merge_unique(*values: Any) -> list[str]:
    """Normalize, merge, and de-duplicate evidence refs in first-seen order."""
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        _append_new_refs(output, seen, normalize_evidence_refs(value))
    return output


def _append_new_refs(output: list[str], seen: set[str], refs: list[str]) -> None:
    for item in refs:
        if item not in seen:
            output.append(item)
            seen.add(item)


def _acceptance_checks(quality_contract: Mapping[str, Any] | None) -> list[str]:
    """Combine default and per-case acceptance checks."""
    checks = list(DEFAULT_ACCEPTANCE_CHECKS)
    if quality_contract:
        _append_acceptance_checks(checks, quality_contract.get("acceptance_checks", []))
    return checks


def _append_acceptance_checks(checks: list[str], items: Any) -> None:
    for item in items:
        text = str(item or "").strip()
        if text and text not in checks:
            checks.append(text)


def _build_work_order_context(
    summary: dict[str, Any],
    refs: list[str],
    route: dict[str, Any],
    quality_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the common context for analyst and reviewer work orders."""
    return {
        "case_summary": render_case_summary(
            {
                "case": summary.get("case", {}),
                "evidence": refs,
                "route": route,
            }
        ),
        "route_summary": route,
        "quality_contract": dict(quality_contract or {}),
    }


def _create_work_orders(*, params: CreateWorkOrdersParams) -> tuple[SubagentWorkOrder, SubagentWorkOrder]:
    """Create analyst and reviewer work orders from common data."""
    analyst = _create_analyst_work_order(params)
    reviewer = _create_reviewer_work_order(params)
    return analyst, reviewer


def _create_analyst_work_order(params: CreateWorkOrdersParams) -> SubagentWorkOrder:
    return SubagentWorkOrder(
        role="analyst",
        case_id=params.case_id,
        goal="Prepare an evidence-backed log analysis report for reviewer gate.",
        mode=params.mode,
        dry_run=params.dry_run,
        ready=params.ready,
        allowed_tools=list(DEFAULT_ANALYST_TOOLS),
        evidence_refs=list(params.refs),
        context={
            **params.common_context,
            "handoff_contract": "AnalystInput",
            "expected_output": "AnalystReport",
        },
        acceptance_checks=list(params.checks),
        issues=list(params.issues),
        risks=list(params.risks),
    )


def _create_reviewer_work_order(params: CreateWorkOrdersParams) -> SubagentWorkOrder:
    return SubagentWorkOrder(
        role="reviewer",
        case_id=params.case_id,
        goal="Review the analyst report against evidence boundaries before parent final approval.",
        mode=params.mode,
        dry_run=params.dry_run,
        ready=params.ready,
        allowed_tools=list(DEFAULT_REVIEWER_TOOLS),
        evidence_refs=list(params.refs),
        context={
            **params.common_context,
            "handoff_contract": "ReviewerInput",
            "expected_input": "AnalystReport from analyst work order",
            "expected_output": "ReviewerDecision",
        },
        acceptance_checks=list(params.checks),
        issues=list(params.issues),
        risks=list(params.risks),
    )


def plan_case_subagent_work_orders(
    case: Any,
    *,
    options: PlanWorkOrdersOptions | None = None,
    evidence_refs: Any = None,
    route_summary: Mapping[str, Any] | None = None,
    quality_contract: Mapping[str, Any] | None = None,
    mode: str = "manual",
    dry_run: bool = True,
) -> LogAnalysisWorkOrderPlan:
    """Generate bounded analyst/reviewer work orders for one log-analysis case."""
    plan_options = options or PlanWorkOrdersOptions(
        evidence_refs=evidence_refs,
        route_summary=route_summary,
        quality_contract=quality_contract,
        mode=str(mode),
        dry_run=bool(dry_run),
    )
    inputs = _plan_inputs(
        case,
        plan_options.evidence_refs,
        plan_options.route_summary,
        plan_options.quality_contract,
    )
    common_context = _build_work_order_context(
        inputs.summary,
        inputs.refs,
        inputs.route,
        plan_options.quality_contract,
    )
    analyst, reviewer = _create_work_orders(
        params=CreateWorkOrdersParams(
            case_id=inputs.case_id,
            common_context=common_context,
            refs=inputs.refs,
            checks=inputs.checks,
            issues=inputs.issues,
            risks=inputs.risks,
            mode=plan_options.mode,
            dry_run=plan_options.dry_run,
            ready=inputs.ready,
        )
    )
    return _plan_from_orders(inputs, [analyst, reviewer], mode=plan_options.mode, dry_run=plan_options.dry_run)


def _plan_from_orders(
    inputs: PlanInputs,
    work_orders: list[SubagentWorkOrder],
    *,
    mode: str,
    dry_run: bool,
) -> LogAnalysisWorkOrderPlan:
    return LogAnalysisWorkOrderPlan(
        case_id=inputs.case_id,
        ready=inputs.ready,
        dry_run=dry_run,
        mode=mode,
        work_orders=work_orders,
        issues=inputs.issues,
        risks=inputs.risks,
    )


def _plan_inputs(
    case: Any,
    evidence_refs: Any,
    route_summary: Mapping[str, Any] | None,
    quality_contract: Mapping[str, Any] | None,
) -> PlanInputs:
    summary = summarize_case(case).to_dict()
    refs = _merge_unique(evidence_refs, _get(case, "evidence_refs") or _get(case, "evidence"), summary.get("evidence"))
    issues, risks = _readiness_notes(refs)
    return PlanInputs(
        summary=summary,
        case_id=_case_id(case, summary),
        route=dict(route_summary or summary.get("route") or {}),
        refs=refs,
        checks=_acceptance_checks(quality_contract),
        issues=issues,
        risks=risks,
        ready=bool(refs),
    )


def _readiness_notes(refs: list[str]) -> tuple[list[str], list[str]]:
    if refs:
        return [], []
    return [NO_EVIDENCE_ISSUE], ["dispatching without evidence_refs would invite unsupported analysis"]


build_log_analysis_work_orders = plan_case_subagent_work_orders

__all__ = [
    "DEFAULT_REVIEWER_TOOLS",
    "NO_EVIDENCE_ISSUE",
    "PLAN_NOT_READY_ISSUE",
    "LogAnalysisWorkOrderPlan",
    "PlanWorkOrdersOptions",
    "SubagentWorkOrder",
    "build_log_analysis_work_orders",
    "plan_case_subagent_work_orders",
]
