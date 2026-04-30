from __future__ import annotations

"""Dry-run work-order planning for log-analysis subagents."""

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Protocol

from ..agents.contracts import (
    DEFAULT_ACCEPTANCE_CHECKS,
    DEFAULT_ANALYST_TOOLS,
    normalize_evidence_refs,
)
from ..agents.summaries import render_case_summary, summarize_case


DEFAULT_REVIEWER_TOOLS = ["evidence_read"]

PARENT_FINAL_GATE = "parent_session_final_approval_required"
NO_EVIDENCE_ISSUE = "case has no evidence_refs; analyst/reviewer work orders are not ready"
PLAN_NOT_READY_ISSUE = "work-order plan is not ready; refusing to create subagent tasks"


class SubAgentTaskCreator(Protocol):
    def create_run(self, **kwargs: Any) -> Any:
        ...


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


@dataclass
class SubagentWorkOrderCreationResult:
    """Result from optionally materializing work orders into SubAgentTask records."""

    case_id: str
    ready: bool
    apply: bool
    dry_run: bool
    mode: str
    created: list[dict[str, Any]] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def _work_order_quality_contract(order: SubagentWorkOrder) -> dict[str, Any]:
    source = order.context.get("quality_contract")
    inherited = dict(source) if isinstance(source, Mapping) else {}
    evidence_required = _merge_unique(inherited.get("evidence_required"), order.evidence_refs)
    must_check = list(order.acceptance_checks)
    for item in inherited.get("must_check", []):
        text = str(item or "").strip()
        if text and text not in must_check:
            must_check.append(text)
    return {
        **inherited,
        "user_visible_goal": inherited.get("user_visible_goal") or order.goal,
        "quality_bar": inherited.get("quality_bar") or "Evidence-backed log analysis for parent final review.",
        "evidence_required": evidence_required,
        "must_check": must_check,
        "final_judge": "parent_final_gate",
        "cannot_self_accept": True,
        "parent_final_gate": True,
    }


def _work_order_context_pack(order: SubagentWorkOrder) -> dict[str, Any]:
    return {
        "name": "log-analysis-work-order",
        "case_id": order.case_id,
        "role": order.role,
        "evidence_refs": list(order.evidence_refs),
        "route_summary": dict(order.context.get("route_summary") or {}),
        "quality_contract": _work_order_quality_contract(order),
        "cannot_self_accept": order.cannot_self_accept,
        "parent_final_gate": order.parent_final_gate,
        "case_summary": order.context.get("case_summary", ""),
        "handoff_contract": order.context.get("handoff_contract", ""),
        "expected_input": order.context.get("expected_input", ""),
        "expected_output": order.context.get("expected_output", ""),
    }


def _work_order_plan_steps(order: SubagentWorkOrder) -> list[str]:
    steps = [
        f"Read the focused context pack for case {order.case_id}.",
        "Use only allowed tools and retained evidence_refs.",
        "Produce the role-specific contract output without claiming final acceptance.",
        "Leave final approval to the parent session gate.",
    ]
    if order.role == "reviewer":
        steps.insert(1, "Check the analyst report against the evidence boundary before any approval recommendation.")
    return steps


def create_subagent_tasks_from_work_order_plan(
    subagents: SubAgentTaskCreator,
    plan: LogAnalysisWorkOrderPlan,
    *,
    apply: bool = False,
    parent_id: str = "",
    root_id: str = "",
    final_owner: str = "parent",
) -> SubagentWorkOrderCreationResult:
    """Create SubAgentTask records from a log-analysis work-order plan.

    This is a manual materialization step only. It never invokes a runner,
    model, or analysis loop.
    """

    mode = "apply" if apply else "dry_run"
    issues = list(plan.issues)
    risks = list(plan.risks)
    if not plan.ready and PLAN_NOT_READY_ISSUE not in issues:
        issues.append(PLAN_NOT_READY_ISSUE)
    result = SubagentWorkOrderCreationResult(
        case_id=plan.case_id,
        ready=plan.ready,
        apply=apply,
        dry_run=not apply,
        mode=mode,
        issues=issues,
        risks=risks,
    )
    if not apply or not plan.ready:
        return result

    for order in plan.work_orders:
        if not order.ready:
            issue = f"{order.role} work order is not ready; skipped"
            if issue not in result.issues:
                result.issues.append(issue)
            continue
        quality_contract = _work_order_quality_contract(order)
        context_pack = _work_order_context_pack(order)
        task = subagents.create_run(
            goal=order.goal,
            thought=(
                f"Manual LOG {order.role} work order for {order.case_id}; "
                "stay evidence-bound and leave final acceptance to the parent."
            ),
            plan=_work_order_plan_steps(order),
            agent_name=f"log-{order.role}",
            role=order.role,
            parent_id=parent_id,
            root_id=root_id,
            allowed_tools=list(order.allowed_tools),
            owner=order.role,
            supervisor=parent_id or "parent",
            final_owner=final_owner,
            acceptance_checks=list(order.acceptance_checks),
            quality_contract=quality_contract,
            context_manifest={
                "task_pack_refs": [
                    f"log-analysis-case:{order.case_id}",
                    *[f"evidence:{ref}" for ref in order.evidence_refs],
                ],
                "role_pack": f"log-analysis-{order.role}",
                "quality_contract_ref": "context_packs[0].quality_contract",
                "omitted_context": ["raw_events", "transcript", "runner_result"],
            },
            context_packs=[context_pack],
        )
        result.task_ids.append(str(task.id))
        result.created.append(
            {
                "task_id": str(task.id),
                "case_id": order.case_id,
                "role": order.role,
                "status": getattr(task, "status", ""),
                "verification_status": getattr(task, "verification_status", ""),
            }
        )
    return result


__all__ = [
    "DEFAULT_REVIEWER_TOOLS",
    "LogAnalysisWorkOrderPlan",
    "NO_EVIDENCE_ISSUE",
    "PARENT_FINAL_GATE",
    "PLAN_NOT_READY_ISSUE",
    "SubagentWorkOrder",
    "SubagentWorkOrderCreationResult",
    "build_log_analysis_work_orders",
    "create_subagent_tasks_from_work_order_plan",
    "plan_case_subagent_work_orders",
]
