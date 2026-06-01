from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.recovery_orchestrator import (
    RecoveryOrchestrationRequest,
)


def test_recovery_orchestrator_records_dispatch_step_for_continue_packet(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="继续整理报告", thought="", plan=["继续写"], role="worker")
    task.status = "FAILED"
    task.acceptance_checks = ["报告存在"]
    _write_continue_packet(task)
    manager.save(task)

    report = manager.orchestrate_recovery(
        params=RecoveryOrchestrationRequest(run_ids=[task.id], apply=False, requested_by="parent-test")
    )

    assert report.dry_run is True
    assert report.summary["requires_dispatch"] == 1
    step = report.steps[0]
    assert step.run_id == task.id
    assert step.orchestration_action == "dispatch_original_run"
    assert step.suggested_tool_call["tool"] == "dispatch_subagents"
    assert step.suggested_tool_call["run_ids"] == [task.id]
    ledger = _ledger_rows(tmp_path)
    assert ledger[-1]["schema_version"] == "subagent_recovery_orchestration.v1"
    assert ledger[-1]["orchestration_action"] == "dispatch_original_run"
    assert ledger[-1]["dry_run"] is True


def test_recovery_orchestrator_can_apply_idempotent_takeover_run(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="接管超时页面任务", thought="", plan=["接着写"], role="worker")
    task.status = "TIMEOUT"
    task.failure_type = "runner_timeout"
    Path(task.agent_run_checkpoint_json).write_text("{}", encoding="utf-8")
    manager.save(task)

    report = manager.orchestrate_recovery(
        params=RecoveryOrchestrationRequest(run_ids=[task.id], apply=True, requested_by="parent-test")
    )

    step = report.steps[0]
    assert report.dry_run is False
    assert step.orchestration_action == "create_takeover_run"
    assert step.applied is True
    assert step.ok is True
    takeover_id = str(step.result_refs[0])
    assert manager.load(takeover_id).attributes["takeover_source_run_id"] == task.id
    assert manager.load(task.id).takeover_by == takeover_id
    ledger = _ledger_rows(tmp_path)
    assert ledger[-1]["run_id"] == task.id
    assert ledger[-1]["applied"] is True


def _write_continue_packet(task) -> Path:
    path = Path(task.agent_run_compactions_dir) / "session" / "latest_continue_packet.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "subagent_continue_packet.v1",
                "run_id": task.id,
                "owner": {"owner_id": task.id},
                "memory_scope": "task_local",
                "writes_main_memory": False,
                "ready_to_continue": True,
                "recommended_read_paths": [],
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _ledger_rows(root: Path) -> list[dict[str, object]]:
    path = root / "subagent_recovery_ledger.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
