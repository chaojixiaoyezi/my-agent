from __future__ import annotations

import json

from agent_py_agent.agent.tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from agent_py_agent.agent.tooling.registry_execution import (
    ExecuteRegistryCallParams,
    execute_registry_call,
)


class EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        category="utility",
        effect="read_only",
        description="Return params.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
    )

    def execute(self, params):
        return ToolExecutionResult("echo", True, json.dumps(params, sort_keys=True))


def test_registry_rejects_case_insensitive_tool_names_without_executing():
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "Echo", "value": 1},
            tools={"echo": EchoTool()},
            workspace_root=__import__("pathlib").Path(".").resolve(),
            workspace_roots=[__import__("pathlib").Path(".").resolve()],
        )
    )

    assert result.ok is False
    runtime_gate = result.result_envelope["runtime_gate"]
    assert runtime_gate["findings"][0]["code"] == "TOOL_NOT_REGISTERED"
    assert runtime_gate["findings"][0]["evidence"]["suggested_tool_name"] == "echo"


def test_registry_unknown_tool_returns_repair_suggestion_without_executing():
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "echp", "value": 1},
            tools={"echo": EchoTool()},
            workspace_root=__import__("pathlib").Path(".").resolve(),
            workspace_roots=[__import__("pathlib").Path(".").resolve()],
        )
    )

    assert result.ok is False
    runtime_gate = result.result_envelope["runtime_gate"]
    assert runtime_gate["findings"][0]["code"] == "TOOL_NOT_REGISTERED"
    assert runtime_gate["findings"][0]["evidence"]["suggested_tool_name"] == "echo"
