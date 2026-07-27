from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.subagent.finalize_helpers import (
    FinalizedRunnerRecordRequest,
    record_finalized_runner_result,
)
from agent_py_agent.agent.agent_core.subagent.params import SubagentFinalizeParams
from agent_py_agent.agent.subagents import SubAgentParsedOutput


def _params(run_id: str = "parent"):
    return SubagentFinalizeParams(
        run_id=run_id,
        active_attempt_id="attempt-1",
        result=SimpleNamespace(
            tool_rounds=3,
            executed_tools=["dispatch_subagents"],
            live_context_compaction={
                "event_count": 2,
                "threshold_tokens": 28_800,
                "all_below_threshold": True,
            },
        ),
        context=SimpleNamespace(goal="协调任务"),
        prompt="",
    )


def _agent(*, child_ids: list[str] | None = None):
    captured = SimpleNamespace(params=None)

    def record_runner_result(params):
        captured.params = params
        return SimpleNamespace(
            status=params.structured_output.status if params.structured_output.found else params.status,
            verification_status=(
                params.structured_output.status if params.structured_output.found else params.verification_status
            ),
        )

    def load(run_id: str):
        if run_id == "parent":
            return SimpleNamespace(id="parent", child_ids=list(child_ids or []))
        return SimpleNamespace(id=run_id, status="BLOCKED", verification_status="FAILED")

    return (
        SimpleNamespace(
            subagents=SimpleNamespace(
                load=load,
                runner_result=SimpleNamespace(record_runner_result=record_runner_result),
            )
        ),
        captured,
    )


def test_record_finalized_runner_result_preserves_child_status_without_parent_gate():
    agent, captured = _agent(child_ids=["child-a"])
    structured = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="DONE",
        summary="子代理已按自己的结果完成。",
    )

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(agent, _params(), structured, _repair_state())
    )

    assert result.status == "DONE"
    assert captured.params.structured_output is structured
    assert captured.params.structured_output.failure_type == ""
    assert captured.params.structured_output.next_actions == []
    assert captured.params.live_context_compaction == {
        "event_count": 2,
        "threshold_tokens": 28_800,
        "all_below_threshold": True,
    }


def test_record_finalized_runner_result_preserves_artifact_status_for_closeout():
    agent, captured = _agent()
    structured = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="DONE",
        summary="产物由通用 closeout 或父代理继续判断。",
        artifacts=[{"path": "artifacts/index.html", "kind": "file"}],
    )

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(agent, _params("worker"), structured, _repair_state())
    )

    assert result.status == "DONE"
    assert captured.params.structured_output is structured
    assert captured.params.structured_output.failure_type == ""


def test_record_finalized_runner_result_fails_closed_when_repair_has_no_structure():
    agent, captured = _agent()
    structured = SubAgentParsedOutput(found=False, ok=False)
    repair_state = _repair_state()
    repair_state.update(
        {
            "attempted": True,
            "error": "repair response still missing structured output",
        }
    )

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(agent, _params("worker"), structured, repair_state)
    )

    assert result.status == "BLOCKED"
    assert result.verification_status == "UNVERIFIED"
    assert captured.params.ok is False
    assert captured.params.failure_type == "structured_output_parse_error"
    assert "repair response still missing" in captured.params.message


def _repair_state() -> dict[str, object]:
    return {
        "message": "runner 已完成模型调用。",
        "prompt_for_log": "",
        "response_for_log": "",
        "backend_name": "test",
        "attempted": False,
        "ok": False,
        "error": "",
    }
