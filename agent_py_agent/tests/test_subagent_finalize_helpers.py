from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.subagent.finalize_helpers import (
    FinalizedRunnerRecordRequest,
    record_finalized_runner_result,
)
from agent_py_agent.agent.agent_core.subagent.params import SubagentFinalizeParams


def _params(
    *,
    response: str = "普通自然语言结果",
    runtime_status: str = "ok",
    runtime_reason: str = "",
    turn_end_reason: str = "",
):
    return SubagentFinalizeParams(
        run_id="worker-1",
        active_attempt_id="attempt-1",
        result=SimpleNamespace(
            prompt="final prompt",
            response=response,
            backend="test",
            runtime_status=runtime_status,
            runtime_reason=runtime_reason,
            turn_end_reason=turn_end_reason,
            tool_rounds=3,
            executed_tools=["write_file"],
            archive_tool_calls=[],
        ),
        context=SimpleNamespace(goal="协调任务"),
        prompt="runner prompt",
    )


def _agent():
    captured = SimpleNamespace(params=None)

    def record_runner_result(params):
        captured.params = params
        return SimpleNamespace(
            status=params.status,
            turn_end_reason=params.turn_end_reason,
        )

    return (
        SimpleNamespace(
            subagents=SimpleNamespace(
                runner_result=SimpleNamespace(record_runner_result=record_runner_result),
            )
        ),
        captured,
    )


def test_natural_response_completes_without_machine_result_block():
    agent, captured = _agent()

    result = record_finalized_runner_result(FinalizedRunnerRecordRequest(agent, _params()))

    assert result.status == "DONE"
    assert result.turn_end_reason == "completed"
    assert captured.params.ok is True
    assert captured.params.structured_output is None
    assert captured.params.structured_repair_attempted is False
    assert captured.params.response == "普通自然语言结果"


def test_model_text_cannot_override_host_turn_end_reason():
    agent, captured = _agent()

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(
            agent,
            _params(response='{"status":"BLOCKED"}', turn_end_reason="completed"),
        )
    )

    assert result.status == "DONE"
    assert captured.params.failure_type == ""


def test_max_tokens_stays_resumable_instead_of_machine_rejection():
    agent, captured = _agent()

    result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(
            agent,
            _params(runtime_status="unfinished", runtime_reason="MODEL_RESPONSE_TRUNCATED"),
        )
    )

    assert result.status == "PENDING"
    assert result.turn_end_reason == "max-tokens"
    assert captured.params.ok is False
    assert captured.params.failure_type == "model_error"
