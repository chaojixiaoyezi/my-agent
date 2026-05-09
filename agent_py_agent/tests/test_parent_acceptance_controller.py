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
from agent_py_agent.agent.subagents.parent_acceptance_auto_execution import (
    ParentAcceptanceAutoExecutionOptions,
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


def _write_output(task, tests, *, patches=None):
    Path(task.output_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "status": "AWAITING_ACCEPTANCE",
                "summary": "worker says tests are ready",
                "tests": tests,
                "artifacts": [],
                "patches": patches or [],
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


def test_parent_acceptance_auto_policy_dry_run_allows_run_tests_without_execution():
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

        policy = agent.subagents.plan_parent_acceptance_auto_policy(task.id)

        payload = json.loads(Path(task.reports_dir, "parent_acceptance_auto_policy.json").read_text(encoding="utf-8"))
        reloaded = agent.subagents.load(task.id)
        assert policy.action == "run_tests"
        assert policy.decision == "allow"
        assert policy.dry_run is True
        assert policy.would_execute is True
        assert policy.executed is False
        assert policy.execution_mode == "manual_only"
        assert policy.automatic_execution_allowed is False
        assert policy.recommended_command == f"subagents-tests {task.id} --re-run"
        assert policy.preflight_status == "manual_ready"
        assert policy.ready_for_manual_execution is True
        assert policy.ready_for_automatic_execution is False
        assert policy.preflight_checks["action_in_allowlist"] is True
        assert policy.preflight_checks["automatic_execution_allowed"] is False
        assert policy.preflight_blockers == ["automatic_execution_disabled"]
        assert policy.command == f"subagents-tests {task.id} --re-run"
        assert policy.mutates_task_state is False
        assert reloaded.status == "AWAITING_ACCEPTANCE"
        assert reloaded.verification_status == "NEEDS_ACCEPTANCE"
        assert payload["schema"] == "parent_acceptance_auto_policy.v1"
        assert payload["dry_run"] is True
        assert payload["policy"]["decision"] == "allow"
        assert payload["policy"]["execution_mode"] == "manual_only"
        assert payload["policy"]["automatic_execution_allowed"] is False
        assert payload["policy"]["recommended_command"] == f"subagents-tests {task.id} --re-run"
        assert payload["policy"]["preflight_status"] == "manual_ready"
        assert payload["policy"]["ready_for_automatic_execution"] is False
        assert payload["reserved"]["refs_only"] is True


def test_parent_acceptance_auto_policy_blocks_human_confirmation():
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

        policy = agent.subagents.plan_parent_acceptance_auto_policy(task.id)

        assert policy.action == "request_human_confirmation"
        assert policy.decision == "blocked"
        assert policy.requires_human_confirmation is True
        assert policy.would_execute is False
        assert policy.executed is False
        assert policy.preflight_status == "blocked"
        assert policy.ready_for_manual_execution is False
        assert policy.ready_for_automatic_execution is False
        assert "requires_human_confirmation" in policy.preflight_blockers


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


def test_parent_acceptance_auto_executor_bundles_are_json_stable():
    from agent_py_agent.agent.subagents.parent_acceptance_auto_execution import (
        ParentAcceptanceAutoExecutionRequest,
        ParentAcceptanceAutoExecutionResult,
    )

    request = ParentAcceptanceAutoExecutionRequest(
        run_id="run-1",
        mode="dry_run",
        policy_ref="reports/parent_acceptance_auto_policy.json",
        recommended_command="subagents-tests run-1 --re-run",
        preflight_status="manual_ready",
        ready_for_manual_execution=True,
        ready_for_automatic_execution=False,
        preflight_blockers=["automatic_execution_disabled"],
    )
    result = ParentAcceptanceAutoExecutionResult(
        run_id="run-1",
        mode=request.mode,
        status="blocked",
        request=request,
        blocked_by=["automatic_execution_disabled"],
    )

    assert request.to_dict()["recommended_command"] == "subagents-tests run-1 --re-run"
    assert result.to_dict()["executed"] is False
    assert result.to_dict()["mutates_task_state"] is False
    assert result.to_dict()["request"]["ready_for_automatic_execution"] is False


def test_parent_acceptance_auto_execution_writes_dry_run_audit_file():
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

        result = agent.subagents.plan_parent_acceptance_auto_execution(task.id)

        execution_path = Path(task.reports_dir) / "parent_acceptance_auto_execution.json"
        payload = json.loads(execution_path.read_text(encoding="utf-8"))
        reloaded = agent.subagents.load(task.id)
        assert result.status == "blocked"
        assert result.executed is False
        assert result.execution_allowed is False
        assert result.guard_status == "blocked"
        assert "auto_executor_dry_run_only" in result.blocked_by
        assert "no_process_execution" in result.safety_boundaries
        assert result.command == f"subagents-tests {task.id} --re-run"
        assert result.request.policy_ref.endswith("parent_acceptance_auto_policy.json")
        assert payload["schema"] == "parent_acceptance_auto_execution.v1"
        assert payload["dry_run"] is True
        assert payload["result"]["executed"] is False
        assert payload["result"]["execution_allowed"] is False
        assert payload["result"]["guard_status"] == "blocked"
        assert payload["reserved"]["executes_command"] is False
        assert reloaded.status == "AWAITING_ACCEPTANCE"
        assert reloaded.verification_status == "NEEDS_ACCEPTANCE"


def test_parent_acceptance_auto_execution_manual_confirm_runs_tests_without_apply():
    root_ctx, agent, task = _agent_and_task()
    with root_ctx:
        root = Path(root_ctx.name)
        (root / "README.md").write_text("manual confirm test evidence\n", encoding="utf-8")
        _write_output(
            task,
            [{
                "name": "readme",
                "validation_method": "file_check",
                "file_path": "README.md",
            }],
        )

        result = agent.subagents.plan_parent_acceptance_auto_execution(
            task.id,
            options=ParentAcceptanceAutoExecutionOptions(execute_tests=True),
        )

        execution_path = Path(task.reports_dir) / "parent_acceptance_auto_execution.json"
        followup_path = Path(task.reports_dir) / "parent_acceptance_auto_followup.json"
        test_report_path = Path(task.reports_dir) / "test_execution.json"
        payload = json.loads(execution_path.read_text(encoding="utf-8"))
        followup_payload = json.loads(followup_path.read_text(encoding="utf-8"))
        test_payload = json.loads(test_report_path.read_text(encoding="utf-8"))
        reloaded = agent.subagents.load(task.id)
        _assert_manual_execution_result(result, task, test_report_path, followup_path)
        assert payload["dry_run"] is False
        assert payload["reserved"]["executes_tests"] is True
        assert payload["reserved"]["mutates_task_state"] is False
        _assert_manual_followup_payload(followup_payload)
        assert test_payload["total_tests"] == 1
        assert test_payload["failed"] == 0
        assert reloaded.status == "AWAITING_ACCEPTANCE"
        assert reloaded.verification_status == "NEEDS_ACCEPTANCE"


# LLM: _assert_manual_execution_result keeps the manual auto-execution test below size limits.
# 函数用途: 断言显式测试执行成功写入 report 和 follow-up，但没有 apply 任务状态。
def _assert_manual_execution_result(result, task, test_report_path: Path, followup_path: Path) -> None:
    assert result.status == "tests_executed"
    assert result.mode == "manual_confirm_execute_tests"
    assert result.execution_allowed is True
    assert result.executed is True
    assert result.mutates_task_state is False
    assert result.test_execution_ref == str(test_report_path)
    assert result.test_failed == 0
    assert result.followup_ref == str(followup_path)
    assert result.followup_status == "ready_for_manual_apply"
    assert result.followup_action == "apply_acceptance"
    assert result.followup_command == f"subagents-acceptance-plan {task.id} --apply-followup"


# LLM: _assert_manual_followup_payload checks the persisted follow-up safety boundaries.
# 函数用途: 断言 follow-up 文件只给下一步建议，不会自己修改 task 状态。
def _assert_manual_followup_payload(payload: dict) -> None:
    assert payload["schema"] == "parent_acceptance_auto_followup.v1"
    assert payload["followup"]["status"] == "ready_for_manual_apply"
    assert payload["followup"]["next_action_mutates_task_state"] is True
    assert payload["reserved"]["mutates_task_state"] is False


def test_parent_acceptance_auto_execution_followup_reports_failed_tests_without_rescue():
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

        result = agent.subagents.plan_parent_acceptance_auto_execution(
            task.id,
            options=ParentAcceptanceAutoExecutionOptions(execute_tests=True),
        )

        followup_path = Path(task.reports_dir) / "parent_acceptance_auto_followup.json"
        followup_payload = json.loads(followup_path.read_text(encoding="utf-8"))
        reloaded = agent.subagents.load(task.id)
        assert result.status == "tests_executed"
        assert result.test_failed == 1
        assert result.followup_status == "needs_manual_rescue"
        assert result.followup_action == "plan_rescue"
        assert result.followup_command == (
            f"subagents-acceptance-plan {task.id} --apply-followup --take-over-by <agent>"
        )
        assert followup_payload["followup"]["test_failed"] == 1
        assert followup_payload["followup"]["failed_tests"][0]["name"] == "missing-readme"
        assert followup_payload["followup"]["next_action"]["action"] == "plan_rescue"
        assert followup_payload["followup"]["reserved"]["auto_starts_rescue"] is False
        assert reloaded.status == "AWAITING_ACCEPTANCE"
        assert reloaded.verification_status == "NEEDS_ACCEPTANCE"
