from __future__ import annotations

from agent_py_agent.agent.subagent_workflows.compiler import (
    WorkflowDispatchPlan,
    WorkflowWorkerSpec,
    compile_workflow,
)
from agent_py_agent.agent.subagent_workflows.models import WorkflowPhase, WorkflowTemplate


def _template(phases: list[WorkflowPhase]) -> WorkflowTemplate:
    return WorkflowTemplate(
        id="example",
        name="Example Workflow",
        solves=["testable work"],
        fit_for=["unit tests"],
        phases=phases,
        final_checks=["caller checks evidence"],
    )


def test_compile_single_worker_template_creates_worker_spec():
    template = _template(
        [
            WorkflowPhase(
                id="implement",
                kind="worker",
                task="Implement the requested change.",
                acceptance=["focused tests pass"],
            )
        ]
    )

    plan = compile_workflow(template, goal="Add compiler coverage")

    assert isinstance(plan, WorkflowDispatchPlan)
    assert plan.template_id == "example"
    assert plan.goal == "Add compiler coverage"
    assert plan.final_checks == ["caller checks evidence"]
    assert len(plan.worker_specs) == 1
    spec = plan.worker_specs[0]
    assert isinstance(spec, WorkflowWorkerSpec)
    assert spec.phase_id == "implement"
    assert spec.role == "worker"
    assert spec.kind == "worker"
    assert spec.goal == "Add compiler coverage"
    assert spec.acceptance_checks == ["focused tests pass"]
    assert spec.depends_on == []


def test_compile_multi_phase_template_preserves_depends_on():
    template = _template(
        [
            WorkflowPhase(id="produce", kind="worker", task="Produce artifact."),
            WorkflowPhase(
                id="critic",
                kind="review",
                task="Critique artifact.",
                depends_on=["produce"],
            ),
            WorkflowPhase(
                id="repair",
                kind="worker",
                task="Repair artifact.",
                depends_on=["critic"],
            ),
        ]
    )

    plan = compile_workflow(template, goal="Ship reviewed work")

    assert [spec.phase_id for spec in plan.worker_specs] == [
        "produce",
        "critic",
        "repair",
    ]
    assert plan.worker_specs[0].depends_on == []
    assert plan.worker_specs[1].depends_on == ["produce"]
    assert plan.worker_specs[2].depends_on == ["critic"]


def test_compile_passes_quality_contract_to_plan_and_specs():
    template = _template([WorkflowPhase(id="verify", kind="worker", task="Verify.")])
    quality_contract = {
        "required_checks": ["pytest"],
        "acceptance": ["no known regression"],
    }

    plan = compile_workflow(
        template,
        goal="Respect quality gate",
        quality_contract=quality_contract,
    )

    assert plan.quality_contract is quality_contract
    assert plan.worker_specs[0].quality_contract is quality_contract


def test_compile_passes_write_boundaries_to_each_spec():
    template = _template(
        [
            WorkflowPhase(id="one", kind="worker", task="First phase."),
            WorkflowPhase(id="two", kind="worker", task="Second phase."),
        ]
    )

    plan = compile_workflow(
        template,
        goal="Stay in bounds",
        allowed_write_roots=["agent_py_agent/agent/subagent_workflows/compiler.py"],
        forbidden_write_roots=["docs", "agent_py_agent/agent/subagents"],
    )

    for spec in plan.worker_specs:
        assert spec.allowed_write_roots == [
            "agent_py_agent/agent/subagent_workflows/compiler.py"
        ]
        assert spec.forbidden_write_roots == [
            "docs",
            "agent_py_agent/agent/subagents",
        ]


def test_compile_passes_context_manifest_to_plan_and_specs():
    template = _template([WorkflowPhase(id="inspect", kind="worker", task="Inspect.")])
    context_manifest = {"files": ["agent_py_agent/agent/subagent_workflows/models.py"]}

    plan = compile_workflow(
        template,
        goal="Use supplied context",
        context_manifest=context_manifest,
    )

    assert plan.context_manifest is context_manifest
    assert plan.worker_specs[0].context_manifest is context_manifest


def test_worker_instructions_include_required_constraints():
    template = _template(
        [
            WorkflowPhase(
                id="implement",
                kind="worker",
                task="Implement with evidence.",
            )
        ]
    )

    plan = compile_workflow(template, goal="Constrained work")
    instructions = plan.worker_specs[0].instructions

    assert "not the only worker" in instructions
    assert "Do not roll back or overwrite changes made by other workers" in instructions
    assert "Leave concrete evidence" in instructions
    assert "Report residual risks" in instructions
