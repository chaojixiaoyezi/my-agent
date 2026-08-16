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


def test_xmlish_pseudo_call_never_becomes_executable():
    result = _adapt(
        '<tool_call><function name="write_file">'
        '<parameter name="path">out.txt</parameter>'
        '<parameter name="content">hello</parameter>'
        "</function></tool_call>"
    )

    assert result.calls == ()


def test_protocol_marker_mentioned_in_prose_does_not_execute():
    result = _adapt(
        "你可以用 [TOOL_CALL] 这个协议标记来调用工具，它后面接 JSON 即可。"
    )

    assert result.calls == ()
    assert result.violations[0].code == "PROTOCOL_VIOLATION"
