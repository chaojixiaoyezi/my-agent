from __future__ import annotations

from agent_py_agent.agent.subagents.models import SubAgentParsedOutput, SubAgentTask
from agent_py_agent.agent.subagents.runner_result_state import (
    RunnerResultFieldParams,
    apply_runner_result_fields,
)


def _task() -> SubAgentTask:
    return SubAgentTask(id="run-1", goal="g", thought="t", plan=["p"])


def _apply(task: SubAgentTask, *, parsed: SubAgentParsedOutput, ok: bool = True, failure_type: str = "") -> None:
    apply_runner_result_fields(
        RunnerResultFieldParams(
            task=task,
            result_meta={
                "ok": ok,
                "message": "",
                "response": "",
                "dry_run": False,
            },
            status_context={
                "status": "",
                "verification_status": "",
                "failure_type": failure_type,
            },
            parsed=parsed,
            now=123.0,
        )
    )


def test_runner_result_state_ignores_unknown_structured_failure_type_when_done() -> None:
    task = _task()
    task.current_step = "模型已生成回复"
    task.current_tool = "write_file"

    _apply(task, parsed=SubAgentParsedOutput(found=True, ok=True, status="DONE", failure_type="INCOMPLETE_OUTPUT"))

    assert task.status == "DONE"
    assert task.failure_type == ""
    assert task.current_step == "已完成"
    assert task.current_tool == ""


def test_runner_result_state_maps_unknown_structured_failure_to_status_reason() -> None:
    task = _task()

    _apply(task, parsed=SubAgentParsedOutput(found=True, ok=True, status="BLOCKED", failure_type="INCOMPLETE_OUTPUT"))

    assert task.status == "BLOCKED"
    assert task.failure_type == "status_blocked"


def test_runner_result_state_displays_typed_capability_wait() -> None:
    task = _task()
    task.capability_requests = [type("Request", (), {"status": "OPEN"})()]
    task.current_step = "模型已生成回复"

    _apply(
        task,
        parsed=SubAgentParsedOutput(found=True, ok=True, status="BLOCKED"),
        failure_type="capability_request",
    )

    assert task.status == "BLOCKED"
    assert task.current_step == "等待父级授权"


def test_runner_result_state_does_not_store_unknown_context_failure_type() -> None:
    task = _task()

    _apply(task, parsed=SubAgentParsedOutput(found=False, ok=False), ok=False, failure_type="error")

    assert task.failure_type == "runner_error"
