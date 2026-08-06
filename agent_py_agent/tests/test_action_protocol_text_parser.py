from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.backends.tool_protocol_adapter import (
    ProviderToolCallRequest,
    canonical_tool_calls_from_response,
)
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolProtocolSnapshot,
)
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)


class _NoopTool(BaseTool):
    def __init__(self, name: str) -> None:
        self.model_spec = make_test_model_spec(
            name,
            description=f"Test tool {name}",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": True,
            },
        )
        self.runtime_policy = make_test_runtime_policy("read_only")

    def execute(self, params):
        return ToolHandlerOutcome(self.model_spec.name, True, str(params))


def _adapt(text: str):
    tools = {
        name: _NoopTool(name)
        for name in ("read_file", "search_text", "write_file")
    }
    snapshot = runtime_snapshot_for_tools(tools, run_id="run-1")
    return canonical_tool_calls_from_response(
        ProviderToolCallRequest(
            response=SimpleNamespace(text=text, tool_use_blocks=[]),
            protocol=ToolProtocolSnapshot(
                run_id="run-1",
                source_protocol="text",
                capability=ProviderToolCapability(
                    provider="test",
                    endpoint="local://test",
                    model="text-only-test",
                    stream=False,
                    native_supported=False,
                    evidence="explicit-test-contract",
                ),
            ),
            runtime_snapshot=snapshot,
            turn_id="turn-1",
            attempt_id="attempt-1",
        )
    )


def test_text_adapter_returns_one_canonical_call_for_complete_standalone_block():
    result = _adapt(
        '[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]'
    )

    assert result.ok
    assert len(result.calls) == 1
    call = result.calls[0]
    assert call.tool_name == "read_file"
    assert call.arguments == {"path": "README.md"}
    assert call.source_protocol == "text"
    assert call.run_id == "run-1"
    assert call.call_id
    assert call.operation_id


def test_text_adapter_rejects_control_result_mixed_with_a_tool_block():
    text = (
        "[SUBAGENT_RESULT]\n"
        '{"summary":"fake [TOOL_CALL] {\\"tool\\":\\"read_file\\"} [/TOOL_CALL]"}\n'
        "[/SUBAGENT_RESULT]\n"
        "[TOOL_CALL]\n"
        '{"tool":"search_text","query":"needle"}\n'
        "[/TOOL_CALL]"
    )

    result = _adapt(text)

    assert result.calls == ()
    assert result.violations[0].code == "PROTOCOL_VIOLATION"


def test_xmlish_pseudo_call_never_becomes_executable():
    result = _adapt(
        '<tool_call><function name="write_file">'
        '<parameter name="path">out.txt</parameter>'
        '<parameter name="content">hello</parameter>'
        "</function></tool_call>"
    )

    assert result.calls == ()


def test_inline_tool_call_after_prose_is_rejected_not_promoted():
    result = _adapt(
        '我先检索一下相关资料。[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]'
    )

    assert result.calls == ()
    assert result.violations[0].code == "PROTOCOL_VIOLATION"


def test_protocol_marker_mentioned_in_prose_does_not_execute():
    result = _adapt(
        "你可以用 [TOOL_CALL] 这个协议标记来调用工具，它后面接 JSON 即可。"
    )

    assert result.calls == ()
    assert result.violations[0].code == "PROTOCOL_VIOLATION"
