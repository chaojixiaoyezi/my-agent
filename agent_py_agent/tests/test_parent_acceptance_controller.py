"""Tests for parent acceptance controller dry-run decisions."""

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.execution_report import (
    TestExecutionReportOptions,
    write_test_execution_report,
)
from agent_py_agent.agent.subagents.models import (
    EvidencePacket,
    TestExecutionRecord,
    VerificationEvidence,
)


def _agent_and_task():
    root_ctx = tempfile.TemporaryDirectory()
    root = Path(root_ctx.name)
    cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
    agent = SimpleAgent(cfg, root)
    task = agent.subagents.create_run(
        goal="parent acceptance controller",
        thought="worker claims tests are ready for parent acceptance",
        plan=["write evidence", "run tests", "wait for parent acceptance"],
        acceptance_checks=["must have real test execution evidence"],
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    agent.subagents.save(task)
    return root_ctx, agent, task


def _write_output(task, tests):
    Path(task.output_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "status": "AWAITING_ACCEPTANCE",
                "summary": "worker says tests are ready",
                "tests": tests,
                "artifacts": [],
                "patches": [],
                "blockers": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    Path(task.runner_result_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "structured_output_found": True,
                "structured_output_ok": True,
                "structured_parse_error": "",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_parent_acceptance_plan_executes_tests_when_report_is_missing():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        _write_output(
            task,
            [{
                "name": "unit",
                "validation_method": "command",
                "command": "python -m pytest -q",
            }],
        )

        decision = agent.subagents.plan_parent_acceptance(task.id)

        assert decision.decision == "execute_tests"
        assert decision.risk_level == "low"
        assert decision.requires_human_confirmation is False
        assert any(ref.kind == "output" for ref in decision.evidence_refs)
        assert "test_execution.json" in decision.reason
        assert decision.to_dict()["decision"] == "execute_tests"


def test_parent_acceptance_plan_can_be_written_as_refs_only_audit_file():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        artifact_path = Path(task.reports_dir) / "large-output.txt"
        artifact_path.write_text("PARENT_ACCEPTANCE_DECISION_MUST_NOT_INLINE_ARTIFACT_BODY\n", encoding="utf-8")
        task.artifact_refs = [str(artifact_path)]
        agent.subagents.save(task)
        _write_output(
            task,
            [{
                "name": "unit",
                "validation_method": "command",
                "command": "python -m pytest -q",
            }],
        )

        decision = agent.subagents.write_parent_acceptance_decision(task.id)

        decision_path = Path(task.reports_dir) / "parent_acceptance_decision.json"
        payload = json.loads(decision_path.read_text(encoding="utf-8"))
        reloaded = agent.subagents.load(task.id)
        assert decision.decision == "execute_tests"
        assert payload["schema"] == "parent_acceptance_decision.v1"
        assert payload["dry_run"] is True
        assert payload["decision"]["decision"] == "execute_tests"
        assert payload["reserved"]["future_apply_supported"] is True
        assert reloaded.status == "AWAITING_ACCEPTANCE"
        assert reloaded.verification_status == "NEEDS_ACCEPTANCE"
        assert "PARENT_ACCEPTANCE_DECISION_MUST_NOT_INLINE_ARTIFACT_BODY" not in json.dumps(payload, ensure_ascii=False)


def test_parent_acceptance_apply_allows_inspect_only_decision():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        _write_output(
            task,
            [{
                "name": "unit",
                "validation_method": "command",
                "command": "python -m pytest -q",
                "ok": True,
            }],
        )
        task.evidence_packets = [
            EvidencePacket(id="packet-1", claim="tests passed", evidence_refs=[task.output_json])
        ]
        task.evidence = [
            VerificationEvidence(kind="command", summary="unit tests passed", command="python -m pytest -q", ok=True)
        ]
        agent.subagents.save(task)
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

        result = agent.subagents.apply_parent_acceptance_decision(task.id, reviewer="parent")

        payload = json.loads(Path(task.reports_dir, "parent_acceptance_apply.json").read_text(encoding="utf-8"))
        reloaded = agent.subagents.load(task.id)
        assert result.applied is True
        assert result.parent_decision == "inspect_only"
        assert result.acceptance_decision == "ACCEPT"
        assert reloaded.status == "DONE"
        assert reloaded.verification_status == "VERIFIED"
        assert payload["applied"] is True
        assert payload["reserved"]["auto_execute_tests"] is False


def test_parent_acceptance_apply_blocks_execute_tests_decision_without_mutation():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        _write_output(
            task,
            [{
                "name": "unit",
                "validation_method": "command",
                "command": "python -m pytest -q",
            }],
        )

        result = agent.subagents.apply_parent_acceptance_decision(task.id, reviewer="parent")

        payload = json.loads(Path(task.reports_dir, "parent_acceptance_apply.json").read_text(encoding="utf-8"))
        reloaded = agent.subagents.load(task.id)
        assert result.applied is False
        assert result.parent_decision == "execute_tests"
        assert "requires explicit next action" in result.message
        assert reloaded.status == "AWAITING_ACCEPTANCE"
        assert reloaded.verification_status == "NEEDS_ACCEPTANCE"
        assert payload["applied"] is False
        assert payload["reserved"]["mutates_task_state"] is False


def test_parent_acceptance_next_action_recommends_explicit_test_run_after_blocked_apply():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        _write_output(
            task,
            [{
                "name": "unit",
                "validation_method": "command",
                "command": "python -m pytest -q",
            }],
        )
        agent.subagents.apply_parent_acceptance_decision(task.id, reviewer="parent")

        action = agent.subagents.plan_parent_acceptance_next_action(task.id)

        reloaded = agent.subagents.load(task.id)
        assert action.action == "run_tests"
        assert action.command == f"subagents-tests {task.id} --re-run"
        assert action.apply_ref.endswith("parent_acceptance_apply.json")
        assert action.mutates_task_state is False
        assert reloaded.status == "AWAITING_ACCEPTANCE"
        assert reloaded.verification_status == "NEEDS_ACCEPTANCE"
        assert action.to_dict()["action"] == "run_tests"


def test_parent_acceptance_next_action_requests_human_for_unsafe_command():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        _write_output(
            task,
            [{
                "name": "unsafe",
                "validation_method": "command",
                "command": "python -m pytest -q; remove-stuff",
            }],
        )

        action = agent.subagents.plan_parent_acceptance_next_action(task.id)

        assert action.action == "request_human_confirmation"
        assert action.requires_human_confirmation is True
        assert "unsafe" in action.reason


def test_parent_acceptance_plan_requires_human_for_unsafe_test_command():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        _write_output(
            task,
            [{
                "name": "unsafe",
                "validation_method": "command",
                "command": "python -m pytest -q; remove-stuff",
            }],
        )

        decision = agent.subagents.plan_parent_acceptance(task.id)

        assert decision.decision == "request_human"
        assert decision.risk_level == "high"
        assert decision.requires_human_confirmation is True
        assert any("unsafe" in item for item in decision.next_actions)


def test_parent_acceptance_plan_inspects_only_when_real_tests_already_passed():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        _write_output(
            task,
            [{
                "name": "unit",
                "validation_method": "command",
                "command": "python -m pytest -q",
            }],
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

        assert decision.decision == "inspect_only"
        assert decision.risk_level == "low"
        assert decision.test_execution_ref.endswith("test_execution.json")
        assert "already passed" in decision.reason
