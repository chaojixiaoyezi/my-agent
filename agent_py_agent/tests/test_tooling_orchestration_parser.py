"""Canonical text-adapter coverage for orchestration tool arguments."""

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


class _ScheduleChildrenTool(BaseTool):
    def __init__(self) -> None:
        self.model_spec = make_test_model_spec(
            "schedule_child_subagents",
            category="orchestration",
            description="Schedule explicit child jobs.",
            input_schema={
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean"},
                    "children": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": True},
                    },
                },
                "required": ["dry_run", "children"],
                "additionalProperties": False,
            },
        )
        self.runtime_policy = make_test_runtime_policy("mutating")

    def execute(self, params):
        return ToolHandlerOutcome(self.model_spec.name, True, str(params))


def test_text_adapter_keeps_flat_orchestration_arguments() -> None:
    tool = _ScheduleChildrenTool()
    snapshot = runtime_snapshot_for_tools(
        {tool.model_spec.name: tool},
        run_id="orchestration-parser-run",
    )
    result = canonical_tool_calls_from_response(
        ProviderToolCallRequest(
            response=SimpleNamespace(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"schedule_child_subagents","dry_run":false,'
                    '"children":[{"goal":"child goal","agent_name":"child"}]}\n'
                    "[/TOOL_CALL]"
                ),
                tool_use_blocks=[],
            ),
            protocol=ToolProtocolSnapshot(
                run_id=snapshot.run_id,
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

    assert result.ok
    assert len(result.calls) == 1
    assert result.calls[0].tool_name == "schedule_child_subagents"
    assert result.calls[0].arguments == {
        "dry_run": False,
        "children": [{"goal": "child goal", "agent_name": "child"}],
    }
