from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.recovery.orchestrator import (
    RecoveryOrchestrationRequest,
    SubAgentRecoveryOrchestrator,
)


def test_recovery_orchestrator_records_dispatch_step_for_checkpoint(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="继续整理报告", thought="", plan=["继续写"], role="worker")
    task.status = "FAILED"
    task.acceptance_checks = ["报告存在"]
    manager.save(task)

    report = manager.hierarchy.orchestrate_recovery(
        params=RecoveryOrchestrationRequest(run_ids=[task.id], apply=False, requested_by="parent-test")
    )

    assert report.dry_run is True
    assert report.summary["requires_dispatch"] == 1
    step = report.steps[0]
    assert step.run_id == task.id
    assert step.orchestration_action == "dispatch_original_run"
    assert step.next_actor == "system_dispatcher"
    assert step.strategy_snapshot["recovery_refs"]
    assert step.suggested_tool_call == {}
    ledger = _ledger_rows(tmp_path)
    assert ledger[-1]["schema_version"] == "subagent_recovery_orchestration.v1"
    assert ledger[-1]["step_index"] == 1
    assert ledger[-1]["orchestration_action"] == "dispatch_original_run"
    assert ledger[-1]["next_actor"] == "system_dispatcher"
    assert ledger[-1]["strategy_snapshot"]["recovery_refs"]
    assert ledger[-1]["dry_run"] is True


def test_recovery_orchestrator_can_apply_idempotent_takeover_run(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="接管超时页面任务", thought="", plan=["接着写"], role="worker")
    task.status = "TIMEOUT"
    task.failure_type = "runner_timeout"
    Path(task.agent_run_checkpoint_json).write_text("{}", encoding="utf-8")
    manager.save(task)

    report = manager.hierarchy.orchestrate_recovery(
        params=RecoveryOrchestrationRequest(run_ids=[task.id], apply=True, requested_by="parent-test")
    )

    step = report.steps[0]
    assert report.dry_run is False
    assert step.orchestration_action == "create_takeover_run"
    assert step.next_actor == "orchestrator"
    assert step.applied is True
    assert step.ok is True
    takeover_id = str(step.result_refs[0])
    assert manager.load(takeover_id).attributes["takeover_source_run_id"] == task.id
    assert manager.load(task.id).takeover_by == takeover_id
    ledger = _ledger_rows(tmp_path)
    assert ledger[-1]["run_id"] == task.id
    assert ledger[-1]["applied"] is True


def test_recovery_orchestrator_reports_recoverable_scan_load_error(tmp_path: Path) -> None:
    class BrokenManager:
        workspace = tmp_path

        def list_runs(self):
            raise ValueError("bad subagent ledger")

    report = SubAgentRecoveryOrchestrator(BrokenManager()).orchestrate(
        RecoveryOrchestrationRequest(requested_by="parent-test")
    )

    assert report.steps == []
    assert report.load_errors
    assert report.load_errors[0]["context"] == "subagent_recovery_orchestration.list_recoverable_runs"
    assert report.load_errors[0]["error"]["category"]
    payload = report.to_dict()
    assert payload["load_errors"][0]["error"]["message"]


def test_recovery_orchestrator_does_not_scan_error_status_alias(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="旧状态别名", thought="", plan=["noop"], role="worker")
    task.status = "ERROR"
    manager.save(task)

    report = manager.hierarchy.orchestrate_recovery(params=RecoveryOrchestrationRequest(requested_by="parent-test"))

    assert report.steps == []
    assert report.summary["total"] == 0


def _ledger_rows(root: Path) -> list[dict[str, object]]:
    path = root / "subagent_recovery_ledger.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
