from __future__ import annotations

import json
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


class EchoTool(BaseTool):
    model_spec = make_test_model_spec(
        "echo",
        category="utility",
        description="Return params.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "additionalProperties": False,
        },
    )
    runtime_policy = make_test_runtime_policy("read_only")

    def __init__(self) -> None:
        self.executions = 0

    def execute(self, params):
        self.executions += 1
        return ToolHandlerOutcome("echo", True, json.dumps(params, sort_keys=True))


def _adapt(tool_name: str):
    tool = EchoTool()
    snapshot = runtime_snapshot_for_tools({"echo": tool})
    result = canonical_tool_calls_from_response(
        ProviderToolCallRequest(
            response=SimpleNamespace(
                text="",
                tool_use_blocks=[
                    {"id": "call-invalid", "name": tool_name, "input": {"value": 1}}
                ],
            ),
            protocol=ToolProtocolSnapshot(
                run_id=snapshot.run_id,
                source_protocol="native",
                capability=ProviderToolCapability(
                    provider="test",
                    endpoint="local://test",
                    model="test-model",
                    stream=False,
                    native_supported=True,
                    evidence="test-contract",
                ),
            ),
            runtime_snapshot=snapshot,
            turn_id="test-turn",
            attempt_id="test-attempt",
        )
    )
    return tool, result


def test_provider_rejects_case_insensitive_tool_names_without_executing():
    tool, result = _adapt("Echo")

    assert result.calls == ()
    assert result.violations[0].code == "TOOL_UNAVAILABLE"
    assert result.violations[0].evidence_preview == "Echo"
    assert tool.executions == 0


def test_provider_rejects_unknown_tool_without_guessing_or_executing():
    tool, result = _adapt("echp")

    assert result.calls == ()
    assert result.violations[0].code == "TOOL_UNAVAILABLE"
    assert result.violations[0].evidence_preview == "echp"
    assert tool.executions == 0
