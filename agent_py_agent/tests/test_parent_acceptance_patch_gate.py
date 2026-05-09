"""Tests for parent acceptance patch-review next-action gates."""

import json
from pathlib import Path

from agent_py_agent.agent.subagents.execution_report import (
    TestExecutionReportOptions,
    write_test_execution_report,
)
from agent_py_agent.agent.subagents.models import TestExecutionRecord
from agent_py_agent.agent.subagents.parent_acceptance_auto_execution import (
    ParentAcceptanceAutoExecutionOptions,
)
from agent_py_agent.tests.test_parent_acceptance_controller import _agent_and_task, _write_output


# LLM: test_parent_acceptance_plan_requires_patch_review_after_real_tests_passed guards the real10 roman finding.
# 函数用途: 父级测试通过但 applied patch 未审核时，下一步必须是 patch review，而不是直接 apply acceptance。
def test_parent_acceptance_plan_requires_patch_review_after_real_tests_passed():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        _write_output(
            task,
            [{
                "name": "unit",
                "validation_method": "command",
                "command": "python -m pytest -q",
            }],
            patches=[{"path": "demo.py", "status": "applied", "summary": "worker changed demo"}],
        )
        write_test_execution_report(
            task.reports_dir,
            [
                TestExecutionRecord(
                    test_name="unit",
                    command="python -m pytest -q",
                    executed=True,
                    exit_code=0,
                    validation_method="command",
                    validation_result={"ok": True},
                )
            ],
            options=TestExecutionReportOptions(executed_at="2026-05-08T12:00:00Z"),
        )

        decision = agent.subagents.plan_parent_acceptance(task.id)
        action = agent.subagents.plan_parent_acceptance_next_action(task.id)
        apply_result = agent.subagents.apply_parent_acceptance_decision(task.id, reviewer="parent")
        reloaded = agent.subagents.load(task.id)

        assert decision.decision == "review_patches"
        assert decision.reserved["unreviewed_applied_patch_count"] == 1
        assert action.action == "review_patches"
        assert action.command == f"subagents-patches --review-apply --run-id {task.id}"
        assert action.mutates_task_state is True
        assert apply_result.applied is False
        assert apply_result.parent_decision == "review_patches"
        assert reloaded.status == "AWAITING_ACCEPTANCE"
        assert reloaded.verification_status == "NEEDS_ACCEPTANCE"


# LLM: test_parent_acceptance_auto_execution_followup_recommends_patch_review_after_passed_tests covers semi-auto handoff.
# 函数用途: 显式 run_tests 通过后，如果 patch 未审核，follow-up 推荐 patch review 命令而不是 apply-followup。
def test_parent_acceptance_auto_execution_followup_recommends_patch_review_after_passed_tests():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        root = Path(root_ctx.name)
        (root / "README.md").write_text("patch review followup\n", encoding="utf-8")
        _write_output(
            task,
            [{
                "name": "readme",
                "validation_method": "file_check",
                "file_path": "README.md",
            }],
            patches=[{"path": "demo.py", "status": "applied", "summary": "worker changed demo"}],
        )

        result = agent.subagents.plan_parent_acceptance_auto_execution(
            task.id,
            options=ParentAcceptanceAutoExecutionOptions(execute_tests=True),
        )

        followup_path = Path(task.reports_dir) / "parent_acceptance_auto_followup.json"
        followup_payload = json.loads(followup_path.read_text(encoding="utf-8"))
        assert result.followup_status == "needs_patch_review"
        assert result.followup_action == "review_patches"
        assert result.followup_command == f"subagents-patches --review-apply --run-id {task.id}"
        assert followup_payload["followup"]["status"] == "needs_patch_review"
        assert followup_payload["followup"]["next_action"]["action"] == "review_patches"
