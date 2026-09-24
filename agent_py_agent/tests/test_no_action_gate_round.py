from __future__ import annotations

"""no-action 结构化闸的执行轮集成回归(复核 seq 339 第 2 点)。

评估判 informational(requires_action=False)时模型仍提出 ToolCall,执行层不进
handler——全部转 TOOL_ACTION_NOT_REQUIRED 拦截结果;连续 _NO_ACTION_GATE_HALT_LIMIT
轮拦截后设 no_action_gate_halt,由 _tool_step_or_limit 收口轮接管。
"""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    _NO_ACTION_GATE_HALT_LIMIT,
    ToolRoundExecutionRequest,
    execute_tool_round,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.tooling.action_policy import ActionDecision
from agent_py_agent.agent.tooling.executor import ToolExecution
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolCall,
    ToolProtocolSnapshot,
    ToolResult,
)


def _text_protocol_snapshot() -> ToolProtocolSnapshot:
    return ToolProtocolSnapshot(
        "no-action-test-run",
        "text",
        ProviderToolCapability(
            provider="test",
            endpoint="local://no-action-gate-test",
            model="test-model",
            stream=False,
            native_supported=False,
            evidence="canonical_test_fixture",
        ),
    )


def _gate_params(assessment=None, actions=()):
    snapshot = SimpleNamespace(
        required_actions=actions,
        required_action_assessment=assessment
        if assessment is not None
        else {"source": "model_structured", "requires_action": False},
    )
    return SimpleNamespace(
        tool_context=[],
        effective_contract_snapshot=snapshot,
        tool_protocol_snapshot=_text_protocol_snapshot(),
    )


def _gate_call(name: str = "write_file") -> ToolCall:
    return ToolCall(
        call_id="no-action-test-call-1",
        tool_name=name,
        arguments={"path": "x.txt", "content": "y"},
        source_protocol="native",
        schema_hash="sha256:no-action-test",
        run_id="no-action-test-run",
        turn_id="no-action-test-run:turn",
        attempt_id="no-action-test-run:attempt",
    )


def _gate_request(params, calls, execute_one, record_one, rounds: int = 1):
    return ToolRoundExecutionRequest(
        SimpleNamespace(),
        params,
        rounds,
        ModelResponse(text="tool round", backend="test"),
        calls,
        execute_one,
        record_one,
    )


def _ok_execution(request) -> ToolExecution:
    return ToolExecution(
        request.call,
        ActionDecision("allow"),
        ToolResult.succeeded(request.call, "ok"),
        ("received", "succeeded"),
    )


def test_no_action_gate_blocks_all_calls_handler_not_executed():
    params = _gate_params()
    executed: list[str] = []
    records: list = []

    def execute_one(request):
        executed.append(request.call.tool_name)
        return _ok_execution(request)

    completed = execute_tool_round(
        _gate_request(params, [_gate_call()], execute_one, records.append)
    )
    assert completed is False
    assert executed == [], "informational 轮模型提出的调用不得进 handler"
    assert len(records) == 1
    result = records[0].result
    assert result.handler_executed is False
    assert result.error_code == "TOOL_ACTION_NOT_REQUIRED"
    assert result.retryable is False
    assert result.effect_outcome == "not_started"
    assert result.ok is False


def test_no_action_gate_injects_structured_notice():
    params = _gate_params()
    execute_tool_round(
        _gate_request(params, [_gate_call()], lambda _r: None, lambda _r: None)
    )
    joined = "\n".join(params.tool_context)
    assert "[tool-system:no-action-gate]" in joined
    assert "TOOL_ACTION_NOT_REQUIRED" in joined
    assert "未执行" in joined


def test_no_action_gate_bounded_halt_after_limit():
    params = _gate_params()
    for rounds in range(1, _NO_ACTION_GATE_HALT_LIMIT):
        execute_tool_round(
            _gate_request(params, [_gate_call()], lambda _r: None, lambda _r: None, rounds=rounds)
        )
        assert getattr(params, "no_action_gate_halt", False) is False, "未达限不设 halt"
    execute_tool_round(
        _gate_request(
            params,
            [_gate_call()],
            lambda _r: None,
            lambda _r: None,
            rounds=_NO_ACTION_GATE_HALT_LIMIT,
        )
    )
    assert getattr(params, "no_action_gate_halt", False) is True, "连续拦截达限设 halt"


def test_no_action_gate_inactive_for_execution_round():
    # requires_action=True 的执行轮照常进 handler,闸不激活
    params = _gate_params(assessment={"source": "model_structured", "requires_action": True})
    executed: list[str] = []
    records: list = []

    def execute_one(request):
        executed.append(request.call.tool_name)
        return _ok_execution(request)

    execute_tool_round(_gate_request(params, [_gate_call()], execute_one, records.append))
    assert executed == ["write_file"]
    assert len(records) == 1
    assert records[0].result.ok is True
    assert "\n".join(params.tool_context).find("no-action-gate") == -1
