"""Tests for parent acceptance follow-up control gates."""

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.subagents.execution_report import (
    TestExecutionReportOptions,
    write_test_execution_report,
)
from agent_py_agent.agent.subagents.models import (
    EvidencePacket,
    TestExecutionRecord,
    VerificationEvidence,
)
from agent_py_agent.agent.subagents.parent_acceptance_auto_execution import (
    ParentAcceptanceAutoExecutionOptions,
)
from agent_py_agent.agent.subagents.parent_acceptance_followup_control import (
    ParentAcceptanceFollowUpControlOptions,
    acceptance_followup_control_result,
)
from agent_py_agent.tests.test_parent_acceptance_controller import _agent_and_task, _write_output


def test_parent_acceptance_followup_apply_completes_after_passed_tests():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        root = Path(root_ctx.name)
        (root / "README.md").write_text("manual followup apply\n", encoding="utf-8")
        _write_output(
            task,
            [{
                "name": "readme",
                "validation_method": "file_check",
                "file_path": "README.md",
            }],
        )
        task.evidence_packets = [
            EvidencePacket(id="packet-1", claim="tests passed", evidence_refs=[task.output_json])
        ]
        task.evidence = [
            VerificationEvidence(kind="command", summary="readme test passed", command="", ok=True)
        ]
        agent.subagents.save(task)
        agent.subagents.plan_parent_acceptance_auto_execution(
            task.id,
            options=ParentAcceptanceAutoExecutionOptions(execute_tests=True),
        )

        result = agent.subagents.apply_parent_acceptance_followup(
            task.id,
            options=ParentAcceptanceFollowUpControlOptions(apply=True, reviewer="parent"),
        )

        control_path = Path(task.reports_dir) / "parent_acceptance_followup_control.json"
        payload = json.loads(control_path.read_text(encoding="utf-8"))
        loaded = agent.subagents.load(task.id)
        assert result.status == "applied_acceptance"
        assert result.action == "apply_acceptance"
        assert result.applied is True
        assert result.mutates_task_state is True
        assert payload["result"]["acceptance_apply_ref"].endswith("parent_acceptance_apply.json")
        assert loaded.status == "DONE"
        assert loaded.verification_status == "VERIFIED"


def test_parent_acceptance_followup_result_does_not_mark_rejected_apply_ok():
    root_ctx, _agent, task = _agent_and_task()
    with root_ctx:
        result = acceptance_followup_control_result(
            task,
            SimpleNamespace(
                applied=True,
                acceptance_decision="REJECT",
                message="acceptance still blocked",
                apply_ref=str(Path(task.reports_dir) / "parent_acceptance_apply.json"),
            ),
        )

        assert result.status == "acceptance_rejected"
        assert result.applied is True
        assert result.ok is False
        assert result.mutates_task_state is True


def test_parent_acceptance_followup_blocks_invalid_json_without_crashing():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        followup_path = Path(task.reports_dir) / "parent_acceptance_auto_followup.json"
        followup_path.write_text("{not-json", encoding="utf-8")

        preview = agent.subagents.plan_parent_acceptance_followup(task.id)
        result = agent.subagents.apply_parent_acceptance_followup(
            task.id,
            options=ParentAcceptanceFollowUpControlOptions(apply=True),
        )

        assert preview.status == "invalid_followup"
        assert result.status == "invalid_followup"
        assert result.applied is False
        assert "invalid" in result.message


def test_parent_acceptance_followup_blocks_stale_test_report():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        root = Path(root_ctx.name)
        (root / "README.md").write_text("manual followup stale report\n", encoding="utf-8")
        _write_output(
            task,
            [{
                "name": "readme",
                "validation_method": "file_check",
                "file_path": "README.md",
            }],
        )
        agent.subagents.plan_parent_acceptance_auto_execution(
            task.id,
            options=ParentAcceptanceAutoExecutionOptions(execute_tests=True),
        )
        write_test_execution_report(
            task.reports_dir,
            [
                TestExecutionRecord(
                    test_name="readme",
                    executed=True,
                    validation_method="file_check",
                    validation_result={"ok": False},
                    error="changed after follow-up",
                )
            ],
            options=TestExecutionReportOptions(executed_at="2026-05-09T00:00:00Z"),
        )

        result = agent.subagents.apply_parent_acceptance_followup(
            task.id,
            options=ParentAcceptanceFollowUpControlOptions(apply=True, reviewer="parent"),
        )

        loaded = agent.subagents.load(task.id)
        assert result.status in {"stale_followup", "followup_test_mismatch"}
        assert result.applied is False
        assert loaded.status == "AWAITING_ACCEPTANCE"
        assert loaded.verification_status == "NEEDS_ACCEPTANCE"


def test_parent_acceptance_followup_rescue_uses_action_gate_for_takeover():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        _write_output(
            task,
            [{
                "name": "missing-readme",
                "validation_method": "file_check",
                "file_path": "README.md",
            }],
        )
        agent.subagents.plan_parent_acceptance_auto_execution(
            task.id,
            options=ParentAcceptanceAutoExecutionOptions(execute_tests=True),
        )

        blocked = agent.subagents.apply_parent_acceptance_followup(
            task.id,
            options=ParentAcceptanceFollowUpControlOptions(apply=True),
        )
        rescued = agent.subagents.apply_parent_acceptance_followup(
            task.id,
            options=ParentAcceptanceFollowUpControlOptions(
                apply=True,
                take_over_by="parent-rescue",
                locked_files=["README.md"],
            ),
        )

        loaded = agent.subagents.load(task.id)
        assert blocked.applied is False
        assert "需要 --take-over-by" in blocked.message
        assert rescued.status == "takeover_recorded"
        assert rescued.action == "plan_rescue"
        assert rescued.applied is True
        assert rescued.mutates_task_state is True
        assert loaded.status == "TAKEN_OVER"
        assert loaded.takeover_by == "parent-rescue"
        assert "README.md" in loaded.locked_files
