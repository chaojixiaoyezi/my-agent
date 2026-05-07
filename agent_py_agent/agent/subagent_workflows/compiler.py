from __future__ import annotations

"""Compile workflow templates into reviewable worker dispatch plans."""

from dataclasses import dataclass, field
from typing import Any

from .models import WorkflowTemplate


@dataclass
class WorkflowWorkerSpec:
    """Reviewable instructions for one workflow phase worker."""

    phase_id: str
    role: str
    kind: str
    goal: str
    instructions: str
    acceptance_checks: list[str] = field(default_factory=list)
    allowed_write_roots: list[str] = field(default_factory=list)
    forbidden_write_roots: list[str] = field(default_factory=list)
    quality_contract: Any = None
    context_manifest: Any = None
    cannot_self_accept: bool = True
    depends_on: list[str] = field(default_factory=list)


@dataclass
class WorkflowDispatchPlan:
    """A compiled workflow that can be inspected before dispatch."""

    template_id: str
    template_name: str
    goal: str
    worker_specs: list[WorkflowWorkerSpec]
    parent_acceptance: list[str] = field(default_factory=list)
    quality_contract: Any = None
    context_manifest: Any = None


@dataclass(frozen=True)
class CompileWorkflowParams:
    # LLM: workflow compile inputs are one bundle before worker specs are expanded.
    goal: str
    quality_contract: Any = None
    context_manifest: Any = None
    allowed_write_roots: list[str] | None = None
    forbidden_write_roots: list[str] | None = None


@dataclass(frozen=True)
class _WorkerSpecRequest:
    template: WorkflowTemplate
    phase: object
    values: CompileWorkflowParams
    allowed_roots: list[str]
    forbidden_roots: list[str]


def compile_workflow(
    template: WorkflowTemplate,
    *,
    params: CompileWorkflowParams | None = None,
    goal: str = "",
    quality_contract: Any = None,
    context_manifest: Any = None,
    allowed_write_roots: list[str] | None = None,
    forbidden_write_roots: list[str] | None = None,
) -> WorkflowDispatchPlan:
    """Compile a template into worker specs without creating SubAgentTask."""

    values = params or CompileWorkflowParams(
        goal, quality_contract, context_manifest, allowed_write_roots, forbidden_write_roots
    )
    allowed_roots = list(values.allowed_write_roots or [])
    forbidden_roots = list(values.forbidden_write_roots or [])
    worker_specs = [
        _worker_spec(_WorkerSpecRequest(template, phase, values, allowed_roots, forbidden_roots))
        for phase in template.phases
    ]

    return WorkflowDispatchPlan(
        template_id=template.id,
        template_name=template.name,
        goal=values.goal,
        worker_specs=worker_specs,
        parent_acceptance=list(template.parent_acceptance),
        quality_contract=values.quality_contract,
        context_manifest=values.context_manifest,
    )


def _worker_spec(request: _WorkerSpecRequest) -> WorkflowWorkerSpec:
    # LLM: phase expansion stays isolated from the public compile facade.
    template = request.template
    phase = request.phase
    values = request.values
    return WorkflowWorkerSpec(
        phase_id=phase.id,
        role=phase.kind,
        kind=phase.kind,
        goal=values.goal,
        instructions=_build_worker_instructions(
            template=template,
            phase_id=phase.id,
            phase_kind=phase.kind,
            phase_task=phase.task,
        ),
        acceptance_checks=list(phase.acceptance),
        allowed_write_roots=list(request.allowed_roots),
        forbidden_write_roots=list(request.forbidden_roots),
        quality_contract=values.quality_contract,
        context_manifest=values.context_manifest,
        cannot_self_accept=True,
        depends_on=list(phase.depends_on),
    )


def _build_worker_instructions(
    *,
    template: WorkflowTemplate,
    phase_id: str,
    phase_kind: str,
    phase_task: str,
) -> str:
    return "\n".join(
        [
            f"Workflow template: {template.id} ({template.name}).",
            f"Phase: {phase_id} ({phase_kind}).",
            f"Phase task: {phase_task}",
            "You are not the only worker; other workers may modify adjacent router, gate, or workflow code in parallel.",
            "Do not roll back or overwrite changes made by other workers.",
            "You cannot self-accept final completion; parent acceptance must make the final call.",
            "Leave concrete evidence for every claimed result, including commands run, files changed, and verification output.",
            "Report residual risks and any deferred or unverified items before handing off.",
        ]
    )
