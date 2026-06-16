from __future__ import annotations

from agent_py_agent.agent.backends.tool_ir import AssistantTurn, ToolCall, ToolResult


def test_toolcall_input_is_structured_dict_not_json_string():
    call = ToolCall(id="toolu_1", name="read_file", input={"path": "README.md"})

    assert call.input == {"path": "README.md"}
    assert isinstance(call.input, dict)


def test_toolcall_is_frozen():
    call = ToolCall(id="x", name="read_file", input={})
    try:
        call.name = "write_file"  # type: ignore[misc]
    except Exception as exc:  # FrozenInstanceError is a dataclasses subclass
        assert "frozen" in str(type(exc)).lower() or "cannot assign" in str(exc).lower()
    else:  # pragma: no cover - frozen dataclass must reject mutation
        raise AssertionError("ToolCall should be immutable")


def test_toolresult_defaults_is_error_false():
    result = ToolResult(tool_call_id="toolu_1", content="ok")

    assert result.is_error is False


def test_assistant_turn_holds_text_and_calls_together():
    turn = AssistantTurn(
        text="let me read it",
        tool_calls=[ToolCall(id="toolu_1", name="read_file", input={"path": "a"})],
    )

    assert turn.text == "let me read it"
    assert turn.tool_calls[0].name == "read_file"


def test_assistant_turn_defaults_are_empty():
    turn = AssistantTurn()

    assert turn.text == ""
    assert turn.tool_calls == []


# --- from_payload: the Step 2 bridge from the existing call dict --------------


def test_from_payload_strips_control_keys_into_structured_input():
    payload = {"tool": "read_file", "call_id": "toolu_7", "path": "README.md", "limit": 50}

    call = ToolCall.from_payload(payload)

    assert call.id == "toolu_7"
    assert call.name == "read_file"
    # control keys (tool/call_id) must NOT leak into input
    assert call.input == {"path": "README.md", "limit": 50}


def test_from_payload_uses_fallback_id_when_payload_has_none():
    payload = {"tool": "write_file", "path": "out.md", "content": "x"}

    call = ToolCall.from_payload(payload, fallback_id="toolu_result_id")

    assert call.id == "toolu_result_id"
    assert call.name == "write_file"
    assert call.input == {"path": "out.md", "content": "x"}


def test_from_payload_handles_tool_name_alias_and_non_dict():
    aliased = ToolCall.from_payload({"tool_name": "search_text", "query": "foo"})
    assert aliased.name == "search_text"
    assert aliased.input == {"query": "foo"}

    degenerate = ToolCall.from_payload("not-a-dict")
    assert degenerate == ToolCall(id="", name="", input={})
