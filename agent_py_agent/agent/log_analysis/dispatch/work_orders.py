from __future__ import annotations

"""Dry-run work-order planning for log-analysis subagents."""

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from ..agents.contracts import (
    DEFAULT_ACCEPTANCE_CHECKS,
    DEFAULT_ANALYST_TOOLS,
    normalize_evidence_refs,
)
from ..agents.summaries import render_case_summary, summarize_case


DEFAULT_REVIEWER_TOOLS = ["evidence_read"]

PARENT_FINAL_GATE = "parent_session_final_approval_required"
NO_EVIDENCE_ISSUE = "case has no evidence_refs; analyst/reviewer work orders are not ready"


@dataclass
class SubagentWorkOrder:
    """Small, auditable work order prepared for a subagent role."""

    role: str
    case_id: str
    goal: str
    mode: str = "manual"
    dry_run: bool = True
    ready: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    acceptance_checks: list[str] = field(default_factory=list)
    cannot_self_accept: bool = True
    parent_final_gate: str = PARENT_FINAL_GATE
    issues: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LogAnalysisWorkOrderPlan:
    """Dry-run plan for analyst/reviewer handoff.

    The plan is descriptive only: it does not create subagent runs, invoke a
    runner, or mutate queue state.
    """

    case_id: str
    ready: bool
    dry_run: bool = True
    mode: str = "manual"
    work_orders: list[SubagentWorkOrder] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["work_orders"] = [order.to_dict() for order in self.work_orders]
        return payload


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _case_id(case: Any, summary: Mapping[str, Any]) -> str:
    summary_case = summary.get("case", {})
    if not isinstance(summary_case, Mapping):
        summary_case = {}
    return str(_get(case, "case_id") or _get(case, "id") or summary_case.get("case_id") or "unknown-case")


def _merge_unique(*values: Any) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in normalize_evidence_refs(value):
            if item not in seen:
                output.append(item)
                seen.add(item)
    return output


def _acceptance_checks(quality_contract: Mapping[str, Any] | None) -> list[str]:
    checks = list(DEFAULT_ACCEPTANCE_CHECKS)
    if quality_contract:
        for item in quality_contract.get("acceptance_checks", []):
            text = str(item or "").strip()
            if text and text not in checks:
                checks.append(text)
    return checks


def plan_case_subagent_work_orders(
    case: Any,
    *,
    evidence_refs: Any = None,
    route_summary: Mapping[str, Any] | None = None,
    quality_contract: Mapping[str, Any] | None = None,
    mode: str = "manual",
    dry_run: bool = True,
) -> LogAnalysisWorkOrderPlan:
    """Build analyst and reviewer work orders without executing them."""

    summary_obj = summarize_case(case)
    summary = summary_obj.to_dict()
    case_id = _case_id(case, summary)
    route = dict(route_summary or summary.get("route") or {})
    refs = _merge_unique(evidence_refs, _get(case, "evidence_refs") or _get(case, "evidence"), summary.get("evidence"))
    checks = _acceptance_checks(quality_contract)

    issues: list[str] = []
    risks: list[str] = []
    ready = bool(refs)
    if not ready:
        issues.append(NO_EVIDENCE_ISSUE)
        risks.append("dispatching without evidence_refs would invite unsupported analysis")

    common_context = {
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

    analyst = SubagentWorkOrder(
        role="analyst",
        case_id=case_id,
        goal="Prepare an evidence-backed log analysis report for reviewer gate.",
        mode=mode,
        dry_run=dry_run,
        ready=ready,
        allowed_tools=list(DEFAULT_ANALYST_TOOLS),
        evidence_refs=list(refs),
        context={
            **common_context,
            "handoff_contract": "AnalystInput",
            "expected_output": "AnalystReport",
        },
        acceptance_checks=list(checks),
        issues=list(issues),
        risks=list(risks),
    )
    reviewer = SubagentWorkOrder(
        role="reviewer",
        case_id=case_id,
        goal="Review the analyst report against evidence boundaries before parent final approval.",
        mode=mode,
        dry_run=dry_run,
        ready=ready,
        allowed_tools=list(DEFAULT_REVIEWER_TOOLS),
        evidence_refs=list(refs),
        context={
            **common_context,
            "handoff_contract": "ReviewerInput",
            "expected_input": "AnalystReport from analyst work order",
            "expected_output": "ReviewerDecision",
        },
        acceptance_checks=list(checks),
        issues=list(issues),
        risks=list(risks),
    )

    return LogAnalysisWorkOrderPlan(
        case_id=case_id,
        ready=ready,
        dry_run=dry_run,
        mode=mode,
        work_orders=[analyst, reviewer],
        issues=issues,
        risks=risks,
    )


build_log_analysis_work_orders = plan_case_subagent_work_orders


__all__ = [
    "DEFAULT_REVIEWER_TOOLS",
    "LogAnalysisWorkOrderPlan",
    "NO_EVIDENCE_ISSUE",
    "PARENT_FINAL_GATE",
    "SubagentWorkOrder",
    "build_log_analysis_work_orders",
    "plan_case_subagent_work_orders",
]
