"""Tests for refs-only subagent runner budget summaries."""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.run_budget import (
    SubagentRunBudgetRequest,
    build_subagent_run_budget_report,
)


def _record_real_runner_result(manager: SubAgentManager, run_id: str) -> None:
    manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=run_id,
            dry_run=False,
            ok=True,
            message="finished",
            prompt="p" * 400,
            response="r" * 200,
            backend="minimax",
            tool_rounds=3,
            status="DONE",
            verification_status="VERIFIED",
        )
    )


def test_budget_report_counts_model_calls_tool_rounds_and_token_estimates(tmp_path: Path):
    """Budget report should summarize shared-tree runner cost without loading artifact bodies."""
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="split", plan=["coordinate"], role="coordinator")
    leaf = manager.create_run(
        goal="leaf",
        thought="build",
        plan=["write"],
        parent_id=root.id,
        root_id=root.id,
    )
    _record_real_runner_result(manager, leaf.id)

    report = build_subagent_run_budget_report(
        SubagentRunBudgetRequest(
            manager=manager,
            root_id=root.id,
            max_model_calls=1,
            max_tool_rounds=2,
            max_prompt_response_tokens=200,
        )
    )

    assert report.totals["model_calls"] == 1
    assert report.totals["tool_rounds"] == 3
    assert report.totals["prompt_response_token_estimate"] == 150
    assert report.exceeded == ["tool_rounds"]
    assert report.records[0].prompt_file.endswith("runner_prompt.md")
    assert report.records[0].response_file.endswith("runner_response.md")


def test_budget_report_skips_dry_runs_by_default(tmp_path: Path):
    """Dry-run prompts should not inflate real model-call budgets unless requested."""
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="dry", thought="preview", plan=["prompt"])
    manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            dry_run=True,
            ok=True,
            message="preview",
            prompt="dry prompt",
        )
    )

    report = build_subagent_run_budget_report(SubagentRunBudgetRequest(manager=manager))

    assert report.totals["runs"] == 0
    assert report.totals["model_calls"] == 0


def test_budget_report_reports_dirty_runner_result_json(tmp_path: Path):
    """Bad runner_result.json should be visible instead of silently lowering totals."""
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="dirty result", thought="bad json", plan=["run"])
    Path(task.runner_result_json).write_text("{bad-runner-result", encoding="utf-8")

    report = build_subagent_run_budget_report(SubagentRunBudgetRequest(manager=manager))

    assert report.totals["runs"] == 0
    (error,) = report.load_errors
    assert error["context"] == "subagent.run_budget.runner_result"
    assert error["run_id"] == task.id
    assert error["path"] == task.runner_result_json


def test_manager_writes_budget_report_files(tmp_path: Path):
    """Manager should persist JSON and Markdown budget summaries."""
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="leaf", thought="build", plan=["write"])
    _record_real_runner_result(manager, task.id)

    report = manager.budget.write_run_budget_report(
        params=SubagentRunBudgetRequest(manager=manager, max_model_calls=1)
    )

    assert report.totals["model_calls"] == 1
    assert (tmp_path / "subagent_run_budget.json").is_file()
    assert (tmp_path / "SUBAGENT_RUN_BUDGET.md").read_text(encoding="utf-8").startswith(
        "# Subagent Run Budget"
    )
