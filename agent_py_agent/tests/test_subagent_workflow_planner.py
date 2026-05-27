from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent_py_agent.__main__ import build_parser
from agent_py_agent.agent.config import load_config
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import QualityContract
from agent_py_agent.agent.subagent_workflows.models import WorkflowPhase, WorkflowTemplate
from agent_py_agent.agent.subagent_workflows.planner import (
    WorkflowPlanConstraints,
    plan_workflow_for_goal,
)
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
        final_checks=["caller checks evidence"],
    )


def _store(*template_ids: str) -> WorkflowTemplateStore:
    return WorkflowTemplateStore(templates={template_id: _template(template_id) for template_id in template_ids})


def _write_config(tmp_path: Path, mode: str = "auto") -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "."\n'
        'model_backend: "echo"\n'
        'subagent_workspace: "subs"\n'
        f'subagent_workflow_mode: "{mode}"\n',
        encoding="utf-8",
    )
    return config_path


def test_plan_workflow_for_goal_composes_router_compiler_and_parent_gate():
    contract = QualityContract(
        must_check=["real artifact"],
        evidence_required=["pytest output"],
        forbidden_delivery=["worker self-accepted PASS"],
    )

    result = plan_workflow_for_goal(
        "Fix the API bug and add regression tests",
        constraints=WorkflowPlanConstraints(
            config=_Config("auto"),
            template_store=_store("single_worker_verified", "code_feature_split"),
            workflow_task_type="code_or_bugfix",
            quality_contract=contract,
            allowed_write_roots=["agent_py_agent/agent/example.py"],
        ),
    )

    assert result.ok is True
    assert result.enabled is True
    assert result.selected_template_id == "code_feature_split"
    assert result.template is not None
    assert result.dispatch_plan is not None
    assert result.dispatch_plan.worker_specs[0].quality_contract is contract
    assert result.dispatch_plan.worker_specs[0].allowed_write_roots == [
        "agent_py_agent/agent/example.py"
    ]
    assert result.dispatch_plan.final_checks == ["caller checks evidence"]


def test_plan_workflow_for_goal_respects_off_mode():
    result = plan_workflow_for_goal(
        "Fix the API bug",
        constraints=WorkflowPlanConstraints(
            config=_Config("off"),
            template_store=_store("single_worker_verified", "code_feature_split"),
        ),
    )

    assert result.ok is False
    assert result.enabled is False
    assert result.selected_template_id == ""
    assert result.template is None
    assert result.dispatch_plan is None


def test_plan_workflow_for_goal_manual_mode_marks_confirmation():
    result = plan_workflow_for_goal(
        "small cleanup task",
        constraints=WorkflowPlanConstraints(
            config=_Config("manual"),
            template_store=_store("single_worker_verified"),
        ),
    )

    assert result.ok is True
    assert result.decision.needs_confirmation is True
    assert result.dispatch_plan is not None


def test_workflow_planning_result_serializes_audit_preview():
    result = plan_workflow_for_goal(
        "Fix the API bug and add regression tests",
        constraints=WorkflowPlanConstraints(
            config=_Config("auto"),
            template_store=_store("single_worker_verified", "code_feature_split"),
            workflow_task_type="code_or_bugfix",
        ),
    )

    payload = result.to_dict()

    assert payload["goal"] == "Fix the API bug and add regression tests"
    assert payload["selected_template_id"] == "code_feature_split"
    assert payload["mode"] == "auto"
    assert payload["needs_confirmation"] is False
    assert payload["worker_count"] == 1
    assert payload["workers"] == [
        {
            "phase_id": "implement",
            "role": "worker",
            "kind": "worker",
            "task": "Implement the requested change.",
            "acceptance_check_count": 1,
            "acceptance_checks": ["focused tests pass"],
            "depends_on": [],
        }
    ]
    assert "final_closeout_checklist" not in payload
    assert payload["issues"] == []


def test_subagents_workflow_plan_cli_json_previews_without_dispatch(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "subagents-workflow-plan",
            "Fix API bug and add tests",
            "--task-type",
            "code_or_bugfix",
            "--json",
        ]
    )

    code = args.func(args)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert args.command == "subagents-workflow-plan"
    assert payload["selected_template_id"] == "code_feature_split"
    assert payload["mode"] == "auto"
    assert payload["enabled"] is True
    assert payload["ok"] is True
    assert payload["needs_confirmation"] is False
    assert payload["worker_count"] >= 1
    assert payload["workers"][0]["role"]
    assert payload["issues"] == []


def test_subagents_workflow_plan_cli_writes_preview_files(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    output_dir = tmp_path / "workflow-preview"
    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "subagents-workflow-plan",
            "Fix API bug and add tests",
            "--task-type",
            "code_or_bugfix",
            "--output-dir",
            str(output_dir),
        ]
    )

    code = args.func(args)
    output = capsys.readouterr().out
    json_path = output_dir / "subagent_workflow_plan_preview.json"
    markdown_path = output_dir / "SUBAGENT_WORKFLOW_PLAN_PREVIEW.md"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")

    assert code == 0
    assert json_path.exists()
    assert markdown_path.exists()
    assert "preview_json=" in output
    assert "preview_markdown=" in output
    assert payload["goal"] == "Fix API bug and add tests"
    assert payload["selected_template_id"] == "code_feature_split"
    assert payload["worker_count"] >= 1
    assert "# Subagent Workflow Plan Preview" in markdown
    assert "## Final Closeout Checklist" not in markdown


def test_subagents_workflow_plan_cli_json_reports_written_preview_paths(tmp_path, capsys):
    config_path = _write_config(tmp_path)
    output_dir = tmp_path / "workflow-preview"
    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "subagents-workflow-plan",
            "Fix API bug and add tests",
            "--task-type",
            "code_or_bugfix",
            "--output-dir",
            str(output_dir),
            "--json",
        ]
    )

    code = args.func(args)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert Path(payload["preview_paths"]["json"]).exists()
    assert Path(payload["preview_paths"]["markdown"]).exists()


def test_subagents_workflow_plan_cli_accepts_template_override(tmp_path, capsys):
    config_path = _write_config(tmp_path, mode="manual")
    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "subagents-workflow-plan",
            "Fix API bug and add tests",
            "--task-type",
            "code_or_bugfix",
            "--template-id",
            "single_worker_verified",
            "--json",
        ]
    )

    code = args.func(args)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["selected_template_id"] == "single_worker_verified"
    assert payload["mode"] == "manual"
    assert payload["needs_confirmation"] is True


def test_subagents_dispatch_cli_plan_writes_workflow_plan_into_existing_task(tmp_path):
    config_path = _write_config(tmp_path, mode="off")
    agent = SimpleAgent(load_config(config_path), tmp_path)
    parent = agent.subagents.create_run(
        goal="Fix API bug and add regression tests",
        thought="等待 CLI dispatch 补做 workflow 规划。",
        plan=["等待规划"],
        attributes={"workflow_task_type": "code_or_bugfix"},
    )

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "subagents-dispatch",
            "--apply",
            "--workflow-mode",
            "plan",
            "--max-runners",
            "0",
        ]
    )
    code = args.func(args)
    loaded = agent.subagents.load(parent.id)

    assert code == 0
    assert loaded.workflow_mode == "plan"
    assert loaded.workflow_template_id == "code_feature_split"
    assert loaded.workflow_plan["ok"] is True
    assert loaded.workflow_child_run_ids == []


def test_subagents_dispatch_cli_auto_spawns_workflow_workers(tmp_path):
    config_path = _write_config(tmp_path, mode="off")
    agent = SimpleAgent(load_config(config_path), tmp_path)
    parent = agent.subagents.create_run(
        goal="Fix API bug and add regression tests",
        thought="等待 CLI dispatch 自动派工。",
        plan=["等待自动派工"],
        attributes={"workflow_task_type": "code_or_bugfix"},
    )

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "subagents-dispatch",
            "--apply",
            "--workflow-mode",
            "auto",
            "--max-runners",
            "0",
        ]
    )
    code = args.func(args)
    loaded = agent.subagents.load(parent.id)

    assert code == 0
    assert loaded.workflow_mode == "auto"
    assert loaded.workflow_plan["ok"] is True
    assert len(loaded.workflow_child_run_ids) == 3
