"""测试 subagents-tests CLI 命令。"""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_py_agent.agent.subagents.execution_report import (
    TestExecutionReportOptions,
    write_test_execution_report,
)
from agent_py_agent.agent.subagents.models import TestExecutionRecord
from agent_py_agent.cli._review import (
    cmd_subagents_acceptance,
    cmd_subagents_acceptance_plan,
    cmd_subagents_tests,
)


class _FakeSubagents:
    def __init__(self, task):
        self._task = task
        self.review_options = None
        self.workspace = Path(task.reports_dir).parent

    def load(self, run_id):
        assert run_id == self._task.id
        return self._task

    def write_acceptance_review_report(self, run_ids=None, options=None):
        self.review_options = options
        return SimpleNamespace(records=[], summary={})

    def write_parent_acceptance_decision(self, run_id):
        return self.plan_parent_acceptance(run_id)

    def apply_parent_acceptance_decision(self, run_id, reviewer="parent", note=""):
        decision = self.plan_parent_acceptance(run_id)
        return SimpleNamespace(
            run_id=run_id,
            applied=False,
            parent_decision=decision.decision,
            acceptance_decision="",
            message="decision requires explicit next action",
            decision_ref=str(Path(self._task.reports_dir) / "parent_acceptance_decision.json"),
            apply_ref=str(Path(self._task.reports_dir) / "parent_acceptance_apply.json"),
            to_dict=lambda: {
                "run_id": run_id,
                "applied": False,
                "parent_decision": decision.decision,
                "message": "decision requires explicit next action",
            },
        )

    def plan_parent_acceptance_next_action(self, run_id):
        assert run_id == self._task.id
        return SimpleNamespace(
            run_id=run_id,
            action="run_tests",
            reason="blocked apply requires explicit test execution",
            command=f"subagents-tests {run_id} --re-run",
            apply_ref=str(Path(self._task.reports_dir) / "parent_acceptance_apply.json"),
            decision_ref=str(Path(self._task.reports_dir) / "parent_acceptance_decision.json"),
            requires_human_confirmation=False,
            mutates_task_state=False,
            to_dict=lambda: {
                "run_id": run_id,
                "action": "run_tests",
                "command": f"subagents-tests {run_id} --re-run",
                "mutates_task_state": False,
            },
        )

    def plan_parent_acceptance_auto_policy(self, run_id):
        assert run_id == self._task.id
        return SimpleNamespace(
            run_id=run_id,
            action="run_tests",
            decision="allow",
            reason="action is in auto-policy allowlist",
            command=f"subagents-tests {run_id} --re-run",
            dry_run=True,
            would_execute=True,
            executed=False,
            execution_mode="manual_only",
            automatic_execution_allowed=False,
            recommended_command=f"subagents-tests {run_id} --re-run",
            preflight_status="manual_ready",
            ready_for_manual_execution=True,
            ready_for_automatic_execution=False,
            preflight_blockers=["automatic_execution_disabled"],
            mutates_task_state=False,
            next_action_ref=str(Path(self._task.reports_dir) / "parent_acceptance_next_action.json"),
            to_dict=lambda: {
                "run_id": run_id,
                "action": "run_tests",
                "decision": "allow",
                "dry_run": True,
                "would_execute": True,
                "executed": False,
                "execution_mode": "manual_only",
                "automatic_execution_allowed": False,
                "recommended_command": f"subagents-tests {run_id} --re-run",
                "preflight_status": "manual_ready",
                "ready_for_manual_execution": True,
                "ready_for_automatic_execution": False,
                "preflight_blockers": ["automatic_execution_disabled"],
            },
        )

    def plan_parent_acceptance_auto_execution(self, run_id):
        assert run_id == self._task.id
        request = SimpleNamespace(
            run_id=run_id,
            mode="dry_run",
            policy_ref=str(Path(self._task.reports_dir) / "parent_acceptance_auto_policy.json"),
            recommended_command=f"subagents-tests {run_id} --re-run",
            ready_for_automatic_execution=False,
            to_dict=lambda: {
                "run_id": run_id,
                "mode": "dry_run",
                "policy_ref": str(Path(self._task.reports_dir) / "parent_acceptance_auto_policy.json"),
                "recommended_command": f"subagents-tests {run_id} --re-run",
                "ready_for_automatic_execution": False,
            },
        )
        return SimpleNamespace(
            run_id=run_id,
            mode="dry_run",
            status="blocked",
            request=request,
            execution_allowed=False,
            guard_status="blocked",
            executed=False,
            mutates_task_state=False,
            command=f"subagents-tests {run_id} --re-run",
            execution_ref=str(Path(self._task.reports_dir) / "parent_acceptance_auto_execution.json"),
            blocked_by=["automatic_execution_disabled", "auto_executor_dry_run_only"],
            safety_boundaries=["dry_run_only", "no_process_execution", "no_task_state_mutation"],
            to_dict=lambda: {
                "run_id": run_id,
                "mode": "dry_run",
                "status": "blocked",
                "request": request.to_dict(),
                "execution_allowed": False,
                "guard_status": "blocked",
                "executed": False,
                "mutates_task_state": False,
                "command": f"subagents-tests {run_id} --re-run",
                "execution_ref": str(Path(self._task.reports_dir) / "parent_acceptance_auto_execution.json"),
                "blocked_by": ["automatic_execution_disabled", "auto_executor_dry_run_only"],
                "safety_boundaries": ["dry_run_only", "no_process_execution", "no_task_state_mutation"],
            },
        )

    def plan_parent_acceptance(self, run_id):
        assert run_id == self._task.id
        return SimpleNamespace(
            run_id=run_id,
            decision="execute_tests",
            risk_level="low",
            requires_human_confirmation=False,
            reason="output.json declares tests but reports/test_execution.json is missing",
            evidence_refs=[
                SimpleNamespace(kind="output", path=self._task.output_json, summary="tests=1")
            ],
            test_execution_ref="",
            failure_handoff_ref="",
            takeover_readiness_ref="",
            next_actions=["run explicit parent acceptance tests", "write test_execution.json"],
            to_dict=lambda: {
                "run_id": run_id,
                "decision": "execute_tests",
                "risk_level": "low",
                "requires_human_confirmation": False,
            },
        )


class _FakeAgent:
    def __init__(self, task, *, acceptance_execute_tests=False, acceptance_test_timeout_seconds=120):
        self.config = SimpleNamespace(
            acceptance_execute_tests=acceptance_execute_tests,
            acceptance_test_timeout_seconds=acceptance_test_timeout_seconds,
        )
        self.subagents = _FakeSubagents(task)


def _args(tmp_path, *, re_run=False):
    return argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id="run-1",
        re_run=re_run,
        timeout=7,
    )


def _task(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    output_json = tmp_path / "output.json"
    output_json.write_text(json.dumps({
        "tests": [{
            "name": "file exists",
            "validation_method": "file_check",
            "file_path": "ok.txt",
        }]
    }), encoding="utf-8")
    (tmp_path / "ok.txt").write_text("ok\n", encoding="utf-8")
    return SimpleNamespace(id="run-1", reports_dir=str(reports), output_json=str(output_json))


def test_subagents_tests_prints_existing_report(tmp_path, capsys):
    task = _task(tmp_path)
    write_test_execution_report(
        task.reports_dir,
        [TestExecutionRecord(test_name="unit", executed=True, validation_result={"ok": True})],
        options=TestExecutionReportOptions(executed_at="2026-05-08T12:00:00Z"),
    )

    with patch("agent_py_agent.cli._review.make_agent", return_value=_FakeAgent(task)):
        result = cmd_subagents_tests(_args(tmp_path))

    out = capsys.readouterr().out
    assert result == 0
    assert "SUBAGENT TESTS" in out
    assert "run_id=run-1" in out
    assert "total=1 executed=1 passed=1 failed=0" in out
    assert "unit" in out


def test_subagents_tests_rerun_invokes_explicit_acceptance_execution(tmp_path, capsys):
    task = _task(tmp_path)
    fake_agent = _FakeAgent(task)

    with patch("agent_py_agent.cli._review.make_agent", return_value=fake_agent):
        result = cmd_subagents_tests(_args(tmp_path, re_run=True))

    out = capsys.readouterr().out
    assert result == 0
    assert fake_agent.subagents.review_options.execute_tests is True
    assert fake_agent.subagents.review_options.test_timeout_seconds == 7
    assert Path(task.reports_dir, "test_execution.json").exists()
    assert "re_run=True" in out
    assert "file exists" in out


def test_subagents_acceptance_uses_configured_real_test_defaults(tmp_path):
    task = _task(tmp_path)
    fake_agent = _FakeAgent(task, acceptance_execute_tests=True, acceptance_test_timeout_seconds=11)
    args = argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id=["run-1"],
        apply=False,
        reviewer="tester",
        note="",
        limit=20,
        execute_tests=None,
        test_timeout=None,
    )

    with patch("agent_py_agent.cli._review.make_agent", return_value=fake_agent):
        result = cmd_subagents_acceptance(args)

    assert result == 0
    assert fake_agent.subagents.review_options.execute_tests is True
    assert fake_agent.subagents.review_options.test_timeout_seconds == 11


def test_subagents_acceptance_cli_override_wins_over_config(tmp_path):
    task = _task(tmp_path)
    fake_agent = _FakeAgent(task, acceptance_execute_tests=True, acceptance_test_timeout_seconds=11)
    args = argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id=["run-1"],
        apply=False,
        reviewer="tester",
        note="",
        limit=20,
        execute_tests=False,
        test_timeout=5,
    )

    with patch("agent_py_agent.cli._review.make_agent", return_value=fake_agent):
        result = cmd_subagents_acceptance(args)

    assert result == 0
    assert fake_agent.subagents.review_options.execute_tests is False
    assert fake_agent.subagents.review_options.test_timeout_seconds == 5


def test_subagents_acceptance_plan_prints_parent_decision(tmp_path, capsys):
    task = _task(tmp_path)
    fake_agent = _FakeAgent(task)
    args = argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id="run-1",
        json=False,
    )

    with patch("agent_py_agent.cli._acceptance_plan.make_agent", return_value=fake_agent):
        result = cmd_subagents_acceptance_plan(args)

    out = capsys.readouterr().out
    assert result == 0
    assert "SUBAGENT ACCEPTANCE PLAN" in out
    assert "run_id=run-1 decision=execute_tests risk=low human=False" in out
    assert "output.json declares tests" in out
    assert "run explicit parent acceptance tests" in out


def test_subagents_acceptance_plan_write_prints_decision_file(tmp_path, capsys):
    task = _task(tmp_path)
    fake_agent = _FakeAgent(task)
    args = argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id="run-1",
        json=False,
        write=True,
    )

    with patch("agent_py_agent.cli._acceptance_plan.make_agent", return_value=fake_agent):
        result = cmd_subagents_acceptance_plan(args)

    out = capsys.readouterr().out
    assert result == 0
    assert "SUBAGENT ACCEPTANCE PLAN" in out
    assert "written=" in out
    assert "parent_acceptance_decision.json" in out


def test_subagents_acceptance_plan_apply_prints_apply_result(tmp_path, capsys):
    task = _task(tmp_path)
    fake_agent = _FakeAgent(task)
    args = argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id="run-1",
        json=False,
        write=False,
        apply=True,
        reviewer="parent",
        note="",
    )

    with patch("agent_py_agent.cli._acceptance_plan.make_agent", return_value=fake_agent):
        result = cmd_subagents_acceptance_plan(args)

    out = capsys.readouterr().out
    assert result == 0
    assert "SUBAGENT ACCEPTANCE APPLY" in out
    assert "run_id=run-1 applied=False parent_decision=execute_tests" in out
    assert "parent_acceptance_apply.json" in out


def test_subagents_acceptance_plan_next_action_prints_recommended_action(tmp_path, capsys):
    task = _task(tmp_path)
    fake_agent = _FakeAgent(task)
    args = argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id="run-1",
        json=False,
        write=False,
        apply=False,
        next_action=True,
    )

    with patch("agent_py_agent.cli._acceptance_plan.make_agent", return_value=fake_agent):
        result = cmd_subagents_acceptance_plan(args)

    out = capsys.readouterr().out
    assert result == 0
    assert "SUBAGENT ACCEPTANCE NEXT ACTION" in out
    assert "run_id=run-1 action=run_tests mutates_task_state=False" in out
    assert "command=subagents-tests run-1 --re-run" in out


def test_subagents_acceptance_plan_auto_policy_prints_policy_decision(tmp_path, capsys):
    task = _task(tmp_path)
    fake_agent = _FakeAgent(task)
    args = argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id="run-1",
        json=False,
        write=False,
        apply=False,
        next_action=False,
        auto_policy=True,
    )

    with patch("agent_py_agent.cli._acceptance_plan.make_agent", return_value=fake_agent):
        result = cmd_subagents_acceptance_plan(args)

    out = capsys.readouterr().out
    assert result == 0
    assert "SUBAGENT ACCEPTANCE AUTO POLICY" in out
    assert "run_id=run-1 action=run_tests decision=allow dry_run=True" in out
    assert "would_execute=True executed=False" in out
    assert "execution_mode=manual_only automatic_execution_allowed=False" in out
    assert "preflight_status=manual_ready manual=True automatic=False" in out
    assert "recommended_command=subagents-tests run-1 --re-run" in out


def test_subagents_acceptance_plan_auto_execution_prints_dry_run_facade(tmp_path, capsys):
    task = _task(tmp_path)
    fake_agent = _FakeAgent(task)
    args = argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id="run-1",
        json=False,
        write=False,
        apply=False,
        next_action=False,
        auto_policy=False,
        auto_execution=True,
    )

    with patch("agent_py_agent.cli._acceptance_plan.make_agent", return_value=fake_agent):
        result = cmd_subagents_acceptance_plan(args)

    out = capsys.readouterr().out
    assert result == 0
    assert "SUBAGENT ACCEPTANCE AUTO EXECUTION" in out
    assert "run_id=run-1 mode=dry_run status=blocked" in out
    assert "execution_allowed=False executed=False" in out
    assert "guard_status=blocked" in out
    assert "command=subagents-tests run-1 --re-run" in out
    assert "blocked_by=automatic_execution_disabled,auto_executor_dry_run_only" in out
