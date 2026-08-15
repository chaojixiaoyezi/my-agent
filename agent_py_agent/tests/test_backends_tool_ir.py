from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agent_py_agent.agent.backends.tool_ir import AssistantTurn, ToolCall, ToolResult, UserTurn
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
)


def test_toolcall_arguments_are_structured_and_canonical() -> None:
    call = canonical_history_call(
        "read_file",
        {"path": "README.md"},
        call_id="toolu_1",
    )

    assert call.arguments == {"path": "README.md"}
    assert isinstance(call.arguments, dict)
    assert call.tool_name == "read_file"
    assert call.call_id == "toolu_1"


def test_toolcall_is_frozen() -> None:
    call = canonical_history_call("read_file", {}, call_id="x")

    with pytest.raises(FrozenInstanceError):
        call.tool_name = "write_file"  # type: ignore[misc]


def test_toolresult_success_is_not_error() -> None:
    call = canonical_history_call("read_file", {}, call_id="toolu_1")
    result = canonical_history_result(call, "ok")

    assert result.is_error is False
    assert result.status == "succeeded"
    assert result.call_id == call.call_id


def test_user_turn_preserves_current_turn_input_text() -> None:
    turn = UserTurn("把标准安装验收补上。")

    assert turn.text == "把标准安装验收补上。"


def test_assistant_turn_holds_text_and_calls_together() -> None:
    turn = AssistantTurn(
        text="let me read it",
        tool_calls=[
            canonical_history_call("read_file", {"path": "a"}, call_id="toolu_1")
        ],
    )

    assert turn.text == "let me read it"
    assert turn.tool_calls[0].tool_name == "read_file"


def test_assistant_turn_defaults_are_empty() -> None:
    turn = AssistantTurn()

    assert turn.text == ""
    assert turn.tool_calls == []


def test_backend_ir_exports_the_single_runtime_contract_types() -> None:
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ToolCall as RuntimeToolCall,
    )
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ToolResult as RuntimeToolResult,
    )

    assert ToolCall is RuntimeToolCall
    assert ToolResult is RuntimeToolResult


def test_raw_payload_cannot_construct_a_canonical_tool_call() -> None:
    with pytest.raises(TypeError):
        ToolCall(  # type: ignore[call-arg]
            tool="read_file",
            call_id="toolu_7",
            path="README.md",
        )
