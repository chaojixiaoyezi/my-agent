from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

from agent_py_agent.agent.local_store import LocalStore
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.cli.local_status_payload import StatusPayloadContext, build_status_payload
from agent_py_agent.cli.local_status_view import StatusPrintContext, print_status_human
from agent_py_agent.cli.subagents import cmd_subagents


def test_status_payload_surfaces_shared_progress_and_failure_handoff(tmp_path) -> None:
    store = LocalStore(tmp_path / "local.db")
    manager = SubAgentManager(tmp_path / "subagents", local_store=store)
    task = manager.create_run(goal="handle failed task", thought="keep refs only", plan=["save"])
    task.status = "FAILED"
    task.failure_type = "tool_output_context_overflow"
    task.latest_summary = "tool output too large"
    task.current_step = "waiting takeover"
    artifact_path = tmp_path / "subagents" / task.id / "reports" / "large-output.txt"
    artifact_path.write_text("STATUS_TAKEOVER_VIEW_MUST_NOT_INLINE_ARTIFACT_BODY\n", encoding="utf-8")
    task.artifact_refs = [str(artifact_path)]
    manager.save(task)
    board = manager.build_board(recent_limit=5)

    payload = build_status_payload(_status_payload_context(tmp_path, store, manager, board))

    panels = payload["subagents"]["shared_progress"]
    assert panels[0]["root_task_id"] == task.id
    assert panels[0]["run_count"] == 1
    assert panels[0]["failure_handoff_refs"] == [task.failure_handoff_json]
    assert panels[0]["takeover_readiness_refs"] == [task.takeover_readiness_json]
    assert panels[0]["takeover_entries"][0]["run_id"] == task.id
    assert panels[0]["takeover_entries"][0]["takeover_readiness_ref"] == task.takeover_readiness_json
    assert task.failure_handoff_json in panels[0]["takeover_entries"][0]["recommended_read_order"]
    assert "STATUS_TAKEOVER_VIEW_MUST_NOT_INLINE_ARTIFACT_BODY" not in json.dumps(payload, ensure_ascii=False)


def test_status_payload_surfaces_acceptance_plan_summary(tmp_path) -> None:
    store = LocalStore(tmp_path / "local.db")
    manager = SubAgentManager(tmp_path / "subagents", local_store=store)
    task = manager.create_run(
        goal="await parent acceptance",
        thought="worker says tests are ready",
        plan=["write output", "wait acceptance"],
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    artifact_path = tmp_path / "subagents" / task.id / "reports" / "large-output.txt"
    artifact_path.write_text("STATUS_ACCEPTANCE_PLAN_MUST_NOT_INLINE_ARTIFACT_BODY\n", encoding="utf-8")
    task.artifact_refs = [str(artifact_path)]
    _write_acceptance_output(task, [{
        "name": "unit",
        "validation_method": "command",
        "command": "python -m pytest -q",
    }])
    manager.save(task)
    board = manager.build_board(recent_limit=5)

    payload = build_status_payload(_status_payload_context(tmp_path, store, manager, board))

    entry = payload["subagents"]["shared_progress"][0]["acceptance_plan_entries"][0]
    assert entry["run_id"] == task.id
    assert entry["decision"] == "execute_tests"
    assert entry["risk_level"] == "low"
    assert entry["requires_human_confirmation"] is False
    assert "test_execution.json" in entry["reason"]
    assert "STATUS_ACCEPTANCE_PLAN_MUST_NOT_INLINE_ARTIFACT_BODY" not in json.dumps(payload, ensure_ascii=False)


def test_status_payload_surfaces_acceptance_next_action_summary(tmp_path) -> None:
    store = LocalStore(tmp_path / "local.db")
    manager = SubAgentManager(tmp_path / "subagents", local_store=store)
    task = manager.create_run(
        goal="await parent acceptance next action",
        thought="worker says tests are ready",
        plan=["write output", "wait acceptance"],
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    artifact_path = tmp_path / "subagents" / task.id / "reports" / "large-output.txt"
    artifact_path.write_text("STATUS_ACCEPTANCE_NEXT_ACTION_MUST_NOT_INLINE_ARTIFACT_BODY\n", encoding="utf-8")
    task.artifact_refs = [str(artifact_path)]
    _write_acceptance_output(task, [{
        "name": "unit",
        "validation_method": "command",
        "command": "python -m pytest -q",
    }])
    manager.save(task)
    board = manager.build_board(recent_limit=5)

    payload = build_status_payload(_status_payload_context(tmp_path, store, manager, board))

    entry = payload["subagents"]["shared_progress"][0]["acceptance_next_action_entries"][0]
    assert entry["run_id"] == task.id
    assert entry["action"] == "run_tests"
    assert entry["reason"] == "blocked apply requires explicit test execution"
    assert entry["command"] == f"subagents-tests {task.id} --re-run"
    assert entry["mutates_task_state"] is False
    assert "STATUS_ACCEPTANCE_NEXT_ACTION_MUST_NOT_INLINE_ARTIFACT_BODY" not in json.dumps(payload, ensure_ascii=False)


def test_status_human_prints_shared_progress_failure_handoff(tmp_path, capsys) -> None:
    panel = _takeover_panel_with_scope("FAILED")
    ctx = StatusPrintContext(
        agent=SimpleNamespace(config=SimpleNamespace(agent_name="demo", subagent_board_limit=5), root=tmp_path),
        paths=SimpleNamespace(root=tmp_path),
        local_stats={"record_count": 0, "event_count": 0, "fts5_enabled": True, "db_path": str(tmp_path / "db.sqlite")},
        board=SimpleNamespace(summary={"total": 1}, hot_list=[], recent=[], shared_progress=[panel]),
        timeline=[],
        gateway_status="stopped",
        pid=None,
        alive=False,
        heartbeat_age=0.0,
        active_work_summary=None,
        request_counts={},
        suggested_actions=[],
    )

    print_status_human(ctx)

    output = capsys.readouterr().out
    assert "Shared Progress" in output
    assert "root-1 runs=2 blocked=1 failure_handoffs=1 takeover_packets=1" in output
    assert "Takeover View" in output
    assert "run-1 status=FAILED step=waiting takeover" in output
    assert "principal=employee-1 conversation=feishu-dm-1" in output
    assert "memory=conversation:feishu-dm-1 config=conversation_overlay global_write=False" in output
    assert "read_order[1]=reports/failure_handoff.json" in output
    assert "Acceptance Plan" in output
    assert "run-1 decision=execute_tests risk=low human=False" in output
    assert "Acceptance Next Action" in output
    assert "run-1 action=run_tests mutates_task_state=False" in output
    assert "command=subagents-tests run-1 --re-run" in output


def test_subagents_board_prints_shared_progress_panel(tmp_path) -> None:
    args = SimpleNamespace(limit=10, all=False, status=None, owner=None, root_id=None)
    board = SimpleNamespace(
        summary={"total": 0},
        hot_list=[],
        recent=[],
        items=[],
        shared_progress=[_takeover_panel_with_scope("BLOCKED")],
    )
    agent = SimpleNamespace(subagents=SimpleNamespace(write_board=lambda options: board, workspace=tmp_path))

    stdout = StringIO()
    with redirect_stdout(stdout):
        with _patch_board_agent(agent):
            result = cmd_subagents(args)

    assert result == 0
    assert "Shared Progress" in stdout.getvalue()
    assert "root-1 runs=2 blocked=1 failure_handoffs=1 takeover_packets=1" in stdout.getvalue()
    assert "Takeover View" in stdout.getvalue()
    assert "run-1 status=BLOCKED step=waiting takeover" in stdout.getvalue()
    assert "Acceptance Plan" in stdout.getvalue()
    assert "run-1 decision=execute_tests risk=low human=False" in stdout.getvalue()
    assert "Acceptance Next Action" in stdout.getvalue()
    assert "run-1 action=run_tests mutates_task_state=False" in stdout.getvalue()
    assert "command=subagents-tests run-1 --re-run" in stdout.getvalue()


def _status_payload_context(tmp_path, store, manager, board) -> StatusPayloadContext:
    agent = SimpleNamespace(
        config=SimpleNamespace(agent_name="demo", subagent_board_limit=5),
        root=tmp_path,
        local_store=store,
        subagents=manager,
    )
    return StatusPayloadContext(
        agent=agent,
        paths=SimpleNamespace(root=tmp_path),
        local_stats={"record_count": 0, "event_count": 0, "fts5_enabled": True, "db_path": str(tmp_path / "db.sqlite")},
        board=board,
        timeline=[],
        pid=None,
        alive=False,
        gateway_status="stopped",
        heartbeat_age=0.0,
        active_work_summary=None,
        request_counts={},
    )


def _takeover_panel_with_scope(status: str) -> dict:
    return {
        "root_task_id": "root-1",
        "run_count": 2,
        "blocked_count": 1,
        "failure_handoff_refs": ["reports/failure_handoff.json"],
        "takeover_readiness_refs": ["reports/takeover_readiness.json"],
        "takeover_entries": [_takeover_entry_with_scope(status)],
        "acceptance_plan_entries": [_acceptance_plan_entry()],
        "acceptance_next_action_entries": [_acceptance_next_action_entry()],
    }


def _takeover_entry_with_scope(status: str) -> dict:
    return {
        "run_id": "run-1",
        "status": status,
        "current_step": "waiting takeover",
        "latest_summary": "tool output too large",
        "failure_handoff_ref": "reports/failure_handoff.json",
        "takeover_readiness_ref": "reports/takeover_readiness.json",
        "runtime_identity": {"effective_principal_id": "employee-1", "conversation_id": "feishu-dm-1"},
        "memory_scope": {"namespace": "conversation:feishu-dm-1"},
        "config_scope": {"scope": "conversation_overlay", "writes_global_config": False},
        "recommended_read_order": [
            "reports/takeover_readiness.json",
            "reports/failure_handoff.json",
            "reports/checkpoint.json",
            "reports/artifacts/manifest.jsonl",
        ],
    }


def _patch_board_agent(agent):
    from unittest.mock import patch

    return patch("agent_py_agent.cli._board.make_agent", return_value=agent)


def _acceptance_plan_entry() -> dict:
    return {
        "run_id": "run-1",
        "decision": "execute_tests",
        "risk_level": "low",
        "requires_human_confirmation": False,
        "reason": "reports/test_execution.json is missing",
        "test_execution_ref": "",
        "failure_handoff_ref": "reports/failure_handoff.json",
        "takeover_readiness_ref": "reports/takeover_readiness.json",
    }


def _acceptance_next_action_entry() -> dict:
    return {
        "run_id": "run-1",
        "action": "run_tests",
        "reason": "blocked apply requires explicit test execution",
        "command": "subagents-tests run-1 --re-run",
        "decision_ref": "",
        "apply_ref": "",
        "test_execution_ref": "",
        "failure_handoff_ref": "reports/failure_handoff.json",
        "takeover_readiness_ref": "reports/takeover_readiness.json",
        "mutates_task_state": False,
    }


def _write_acceptance_output(task, tests: list[dict]) -> None:
    from pathlib import Path

    Path(task.output_json).write_text(
        json.dumps({
            "run_id": task.id,
            "status": task.status,
            "summary": "worker says tests are ready",
            "tests": tests,
            "artifacts": task.artifact_refs,
            "patches": [],
            "blockers": [],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
