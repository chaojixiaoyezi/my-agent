from __future__ import annotations

from dataclasses import dataclass

from agent_py_agent.agent.subagent import QualityContract
from agent_py_agent.agent.subagent_workflows.models import WorkflowPhase, WorkflowTemplate
from agent_py_agent.agent.subagent_workflows.planner import plan_workflow_for_goal
from agent_py_agent.agent.subagent_workflows.store import WorkflowTemplateStore


@dataclass
class _Config:
    subagent_workflow_mode: str = "auto"


def _template(template_id: str) -> WorkflowTemplate:
    return WorkflowTemplate(
        id=template_id,
        name=template_id.replace("_", " ").title(),
        solves=["test"],
        fit_for=["test"],
        phases=[
            WorkflowPhase(
                id="implement",
                kind="worker",
                task="Implement the requested change.",
                acceptance=["focused tests pass"],
            )
        ],
        parent_acceptance=["parent checks evidence"],
    )


def _store(*template_ids: str) -> WorkflowTemplateStore:
    return WorkflowTemplateStore(templates={template_id: _template(template_id) for template_id in template_ids})


def test_plan_workflow_for_goal_composes_router_compiler_and_parent_gate():
    contract = QualityContract(
        must_check=["real artifact"],
        evidence_required=["pytest output"],
        forbidden_delivery=["worker self-accepted PASS"],
    )

    result = plan_workflow_for_goal(
        "Fix the API bug and add regression tests",
        config=_Config("auto"),
        template_store=_store("single_worker_verified", "code_feature_split"),
        quality_contract=contract,
        allowed_write_roots=["agent_py_agent/agent/example.py"],
    )

    assert result.ok is True
    assert result.enabled is True
    assert result.selected_template_id == "code_feature_split"
    assert result.template is not None
    assert result.dispatch_plan is not None
    assert result.parent_acceptance_plan is not None
    assert result.dispatch_plan.worker_specs[0].quality_contract is contract
    assert result.dispatch_plan.worker_specs[0].allowed_write_roots == [
        "agent_py_agent/agent/example.py"
    ]
    assert "real artifact" in result.parent_acceptance_plan.checklist
    assert any("worker self-accepted PASS" in item for item in result.parent_acceptance_plan.checklist)


def test_plan_workflow_for_goal_respects_off_mode():
    result = plan_workflow_for_goal(
        "Fix the API bug",
        config=_Config("off"),
        template_store=_store("single_worker_verified", "code_feature_split"),
    )

    assert result.ok is False
    assert result.enabled is False
    assert result.selected_template_id == ""
    assert result.template is None
    assert result.dispatch_plan is None
    assert result.parent_acceptance_plan is None


def test_plan_workflow_for_goal_manual_mode_marks_confirmation():
    result = plan_workflow_for_goal(
        "small cleanup task",
        config=_Config("manual"),
        template_store=_store("single_worker_verified"),
    )

    assert result.ok is True
    assert result.decision.needs_confirmation is True
    assert result.dispatch_plan is not None
