from __future__ import annotations

"""Compose workflow routing, compilation, and parent acceptance planning."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .acceptance import ParentAcceptancePlan, plan_parent_acceptance
from .compiler import WorkflowDispatchPlan, compile_workflow
from .models import WorkflowTemplate
from .router import WorkflowRouteDecision, route_workflow
from .store import WorkflowTemplateStore, load_template_store


@dataclass
class WorkflowPlanConstraints:
    """Bundle for plan_workflow_for_goal keyword parameters (excluding goal)."""

    config: Any = None
    template_store: WorkflowTemplateStore | None = None
    explicit_template_id: str = ""
    quality_contract: Any = None
    context_manifest: Any = None
    allowed_write_roots: list[str] | None = None
    forbidden_write_roots: list[str] | None = None


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

    def to_dict(self) -> dict[str, object]:
        """Return the audit-friendly dry-run preview payload."""

        dispatch_plan = self.dispatch_plan
        parent_acceptance_plan = self.parent_acceptance_plan
        template_phase_tasks = {
            phase.id: phase.task for phase in self.template.phases
        } if self.template is not None else {}
        workers = []
        if dispatch_plan is not None:
            workers = [
                {
                    "phase_id": worker.phase_id,
                    "role": worker.role,
                    "kind": worker.kind,
                    "task": _worker_task_summary(worker, template_phase_tasks),
                    "acceptance_check_count": len(worker.acceptance_checks),
                    "acceptance_checks": list(worker.acceptance_checks),
                    "depends_on": list(worker.depends_on),
                }
                for worker in dispatch_plan.worker_specs
            ]
        parent_checklist = parent_acceptance_plan.checklist if parent_acceptance_plan is not None else []
        return {
            "goal": self.goal,
            "selected_template_id": self.selected_template_id,
            "mode": self.decision.mode,
            "needs_confirmation": self.decision.needs_confirmation,
            "enabled": self.enabled,
            "ok": self.ok,
            "reason": self.decision.reason,
            "task_type": self.decision.task_type,
            "risk_tags": list(self.decision.risk_tags),
            "worker_count": len(workers),
            "workers": workers,
            "parent_acceptance_check_count": len(parent_checklist),
            "parent_acceptance_checklist": list(parent_checklist),
            "issues": list(self.issues),
        }


def plan_workflow_for_goal(
    goal: str,
    *,
    constraints: WorkflowPlanConstraints,
) -> WorkflowPlanningResult:
    """Create a dry-run workflow plan without creating subagent tasks.

    Use WorkflowPlanConstraints to bundle all parameters.
    """
    _c = constraints
    store = _c.template_store or load_template_store()
    decision = route_workflow(
        goal,
        config=_c.config,
        template_store=store,
        explicit_template_id=_c.explicit_template_id,
    )
    issues = list(decision.issues)

    if decision.mode == "off" or not decision.selected_template_id:
        return _make_disabled_result(goal, decision, issues)

    template = store.get(decision.selected_template_id)
    if template is None:
        issues.append(f"selected workflow template not found: {decision.selected_template_id}")
        return _make_disabled_result(goal, decision, issues)

    dispatch_plan, parent_acceptance_plan = _compile_plans(
        template,
        goal=goal,
        quality_contract=_c.quality_contract,
        context_manifest=_c.context_manifest,
        allowed_write_roots=_c.allowed_write_roots,
        forbidden_write_roots=_c.forbidden_write_roots,
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


def _make_disabled_result(
    goal: str,
    decision: WorkflowRouteDecision,
    issues: list[str],
) -> WorkflowPlanningResult:
    """Return a disabled result for early-exit paths."""
    return WorkflowPlanningResult(
        goal=goal,
        decision=decision,
        enabled=False,
        issues=issues,
    )


def _compile_plans(
    template: WorkflowTemplate,
    *,
    goal: str,
    quality_contract: Any,
    context_manifest: Any,
    allowed_write_roots: list[str] | None,
    forbidden_write_roots: list[str] | None,
) -> tuple[WorkflowDispatchPlan, ParentAcceptancePlan]:
    """Compile both dispatch and parent-acceptance plans from a resolved template."""
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
    return dispatch_plan, parent_acceptance_plan


def write_workflow_plan_preview(
    result: WorkflowPlanningResult,
    output_dir: str | Path,
) -> dict[str, Path]:
    """Write the dry-run workflow plan preview as JSON and Markdown."""

    preview_dir = Path(output_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)
    json_path = preview_dir / "subagent_workflow_plan_preview.json"
    markdown_path = preview_dir / "SUBAGENT_WORKFLOW_PLAN_PREVIEW.md"
    payload = result.to_dict()

    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_render_workflow_plan_preview_markdown(payload), encoding="utf-8")
    return {"json": json_path, "markdown": markdown_path}


def _render_workflow_plan_preview_markdown(payload: dict[str, object]) -> str:
    lines = _workflow_plan_header_lines(payload)
    lines.extend(_workflow_plan_worker_lines(payload.get("workers")))
    lines.extend(["", "## Parent Acceptance Checklist"])
    lines.extend(_workflow_plan_list_lines(payload.get("parent_acceptance_checklist")))
    lines.extend(["", "## Issues"])
    lines.extend(_workflow_plan_list_lines(payload.get("issues")))
    lines.append("")
    return "\n".join(lines)


def _workflow_plan_header_lines(payload: dict[str, object]) -> list[str]:
    return [
        "# Subagent Workflow Plan Preview",
        "",
        f"- goal: {payload['goal']}",
        f"- selected_template_id: {payload['selected_template_id'] or 'none'}",
        f"- mode: {payload['mode']}",
        f"- needs_confirmation: {payload['needs_confirmation']}",
        f"- enabled: {payload['enabled']}",
        f"- ok: {payload['ok']}",
        f"- task_type: {payload['task_type']}",
        f"- reason: {payload['reason']}",
        "",
        "## Workers",
    ]


def _workflow_plan_worker_lines(workers: object) -> list[str]:
    lines: list[str] = []
    if not isinstance(workers, list) or not workers:
        return ["", "No workers would be planned."]

    for worker in workers:
        if not isinstance(worker, dict):
            continue
        depends_on = ", ".join(str(item) for item in worker.get("depends_on", [])) or "none"
        lines.extend(
            [
                "",
                f"### {worker.get('phase_id', '')}",
                f"- role: {worker.get('role', '')}",
                f"- kind: {worker.get('kind', '')}",
                f"- depends_on: {depends_on}",
                f"- task: {worker.get('task', '')}",
                f"- acceptance_check_count: {worker.get('acceptance_check_count', 0)}",
            ]
        )
    return lines


def _workflow_plan_list_lines(value: object) -> list[str]:
    if isinstance(value, list) and value:
        return [f"- {item}" for item in value]
    return ["- none"]


def _worker_task_summary(worker: Any, template_phase_tasks: dict[str, str]) -> str:
    task = template_phase_tasks.get(worker.phase_id)
    if task:
        return task
    for line in worker.instructions.splitlines():
        if line.startswith("Phase task: "):
            return line.removeprefix("Phase task: ")
    return worker.instructions.splitlines()[0] if worker.instructions else ""
