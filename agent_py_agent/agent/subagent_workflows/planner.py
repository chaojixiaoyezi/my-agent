from __future__ import annotations

"""Compose workflow routing, compilation, and parent acceptance planning."""

from dataclasses import dataclass, field
from typing import Any

from .acceptance import ParentAcceptancePlan, plan_parent_acceptance
from .compiler import WorkflowDispatchPlan, compile_workflow
from .models import WorkflowTemplate
from .router import WorkflowRouteDecision, route_workflow
from .store import WorkflowTemplateStore, load_template_store


@dataclass
class WorkflowPlanningResult:
    """A parent-reviewable workflow plan for a user goal."""

    goal: str
    decision: WorkflowRouteDecision
    enabled: bool
    template: WorkflowTemplate | None = None
    dispatch_plan: WorkflowDispatchPlan | None = None
    parent_acceptance_plan: ParentAcceptancePlan | None = None
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.enabled and self.template is not None and self.dispatch_plan is not None

    @property
    def selected_template_id(self) -> str:
        return self.decision.selected_template_id


def plan_workflow_for_goal(
    goal: str,
    *,
    config: Any = None,
    template_store: WorkflowTemplateStore | None = None,
    explicit_template_id: str = "",
    quality_contract: Any = None,
    context_manifest: Any = None,
    allowed_write_roots: list[str] | None = None,
    forbidden_write_roots: list[str] | None = None,
) -> WorkflowPlanningResult:
    """Create a dry-run workflow plan without creating subagent tasks."""

    store = template_store or load_template_store()
    decision = route_workflow(
        goal,
        config=config,
        template_store=store,
        explicit_template_id=explicit_template_id,
    )
    issues = list(decision.issues)

    if decision.mode == "off" or not decision.selected_template_id:
        return WorkflowPlanningResult(
            goal=goal,
            decision=decision,
            enabled=False,
            issues=issues,
        )

    template = store.get(decision.selected_template_id)
    if template is None:
        issues.append(f"selected workflow template not found: {decision.selected_template_id}")
        return WorkflowPlanningResult(
            goal=goal,
            decision=decision,
            enabled=False,
            issues=issues,
        )

    dispatch_plan = compile_workflow(
        template,
        goal=goal,
        quality_contract=quality_contract,
        context_manifest=context_manifest,
        allowed_write_roots=allowed_write_roots,
        forbidden_write_roots=forbidden_write_roots,
    )
    parent_acceptance_plan = plan_parent_acceptance(
        template,
        goal=goal,
        quality_contract=quality_contract,
    )

    return WorkflowPlanningResult(
        goal=goal,
        decision=decision,
        enabled=True,
        template=template,
        dispatch_plan=dispatch_plan,
        parent_acceptance_plan=parent_acceptance_plan,
        issues=issues,
    )
