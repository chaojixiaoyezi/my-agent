from __future__ import annotations

from dataclasses import dataclass

from agent_py_agent.agent.subagent_workflows.models import WorkflowTemplate
from agent_py_agent.agent.subagent_workflows.router import WorkflowRouteRequest, route_workflow
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
        phases=[],
        final_checks=["test"],
    )


def _store(*template_ids: str) -> WorkflowTemplateStore:
    return WorkflowTemplateStore(templates={template_id: _template(template_id) for template_id in template_ids})


def _route(goal: str, **kwargs):
    return route_workflow(WorkflowRouteRequest(goal=goal, **kwargs))


def test_off_mode_does_not_select_template():
    decision = _route(
        "small cleanup task",
        config=_Config("off"),
        template_store=_store("single_worker_verified", "code_feature_split"),
    )

    assert decision.mode == "off"
    assert decision.selected_template_id == ""
    assert decision.needs_confirmation is False
    assert "disabled" in decision.reason
    assert decision.available_template_ids == ["code_feature_split", "single_worker_verified"]


def test_manual_mode_recommends_template_and_requires_confirmation():
    decision = _route(
        "small cleanup task",
        config=_Config("manual"),
        template_store=_store("single_worker_verified", "code_feature_split"),
    )

    assert decision.mode == "manual"
    assert decision.selected_template_id == "single_worker_verified"
    assert decision.needs_confirmation is True
    assert "confirmation" in decision.reason


def test_auto_mode_selects_without_confirmation():
    decision = _route(
        "small cleanup task",
        config=_Config("auto"),
        template_store=_store("single_worker_verified", "code_feature_split"),
    )

    assert decision.mode == "auto"
    assert decision.selected_template_id == "single_worker_verified"
    assert decision.needs_confirmation is False


def test_explicit_template_id_takes_priority():
    decision = _route(
        "small cleanup task",
        config=_Config("auto"),
        template_store=_store("single_worker_verified", "producer_critic_repair"),
        explicit_template_id="producer_critic_repair",
    )

    assert decision.selected_template_id == "producer_critic_repair"
    assert decision.issues == []
    assert "Explicit" in decision.reason


def test_missing_explicit_template_records_issue_and_falls_back():
    decision = _route(
        "small cleanup task",
        config=_Config("auto"),
        template_store=_store("single_worker_verified", "producer_critic_repair"),
        explicit_template_id="missing_template",
    )

    assert decision.selected_template_id == "single_worker_verified"
    assert any(issue == "explicit workflow template not found: missing_template" for issue in decision.issues)


def test_code_or_bugfix_task_selects_code_feature_split():
    decision = _route(
        "Fix the API bug and add regression tests",
        config=_Config("auto"),
        template_store=_store("single_worker_verified", "code_feature_split", "producer_critic_repair"),
        workflow_task_type="code_or_bugfix",
    )

    assert decision.task_type == "code_or_bugfix"
    assert decision.selected_template_id == "code_feature_split"
    assert "explicit_workflow" in decision.risk_tags


def test_quality_delivery_task_selects_producer_critic_repair():
    decision = _route(
        "Prepare a high quality PDF report with UI polish",
        config=_Config("auto"),
        template_store=_store("single_worker_verified", "code_feature_split", "producer_critic_repair"),
        workflow_task_type="quality_deliverable",
    )

    assert decision.task_type == "quality_deliverable"
    assert decision.selected_template_id == "producer_critic_repair"
    assert "explicit_workflow" in decision.risk_tags


def test_natural_language_quality_delivery_does_not_select_special_template():
    decision = _route(
        "把 PDF 论文翻译成高质量正式交付报告",
        config=_Config("auto"),
        template_store=_store("single_worker_verified", "code_feature_split", "producer_critic_repair"),
    )

    assert decision.task_type == "simple"
    assert decision.selected_template_id == "single_worker_verified"


def test_explicit_template_arg_selects_template_without_keyword_matching():
    decision = _route(
        "开发日志分析模块的下一步功能并补充测试",
        config=_Config("auto"),
        template_store=_store("single_worker_verified", "code_feature_split", "producer_critic_repair"),
        explicit_template_id="code_feature_split",
    )

    assert decision.task_type == "simple"
    assert decision.selected_template_id == "code_feature_split"


def test_missing_target_template_records_issue_without_guessing_another_template():
    decision = _route(
        "Fix the API bug and add regression tests",
        config=_Config("auto"),
        template_store=_store("single_worker_verified"),
        workflow_task_type="code_or_bugfix",
    )

    assert decision.selected_template_id == ""
    assert any(issue == "preferred workflow template not available: code_feature_split" for issue in decision.issues)
