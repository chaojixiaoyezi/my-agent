from __future__ import annotations

import json

from agent_py_agent.agent.capability.grants import CapabilityGrantScope
from agent_py_agent.agent.capability.mcp import McpToolDescriptor
from agent_py_agent.agent.capability.mcp_runtime import (
    InMemoryMcpExecutor,
    McpExecutionRequest,
    McpTool,
)


def test_mcp_tool_executes_registered_executor_and_returns_structured_payload():
    executor = InMemoryMcpExecutor()
    executor.register("browser", "screenshot", lambda args: {"image_path": args["path"]})
    tool = McpTool(
        descriptor=McpToolDescriptor(
            server="browser",
            name="screenshot",
            description="Capture a browser screenshot",
            capabilities=["browser", "screenshot"],
            keywords=["ui"],
        ),
        executor=executor,
    )

    result = tool.execute({"arguments": {"path": "out.png"}})
    payload = json.loads(result.output)

    assert result.ok is True
    assert result.tool == "mcp.browser.screenshot"
    assert payload["schema_version"] == "mcp_tool_result.v1"
    assert payload["server"] == "browser"
    assert payload["name"] == "screenshot"
    assert payload["result"] == {"image_path": "out.png"}
    assert tool.spec.name == "mcp.browser.screenshot"
    assert tool.spec.category == "mcp"


def test_mcp_tool_rejects_call_outside_grant_scope():
    executor = InMemoryMcpExecutor()
    executor.register("browser", "screenshot", lambda args: {"ok": True})
    scope = CapabilityGrantScope(mcp_tools=["browser.navigate"])
    tool = McpTool(
        descriptor=McpToolDescriptor(server="browser", name="screenshot", description="Screenshot"),
        executor=executor,
        grant_scope=scope,
    )

    result = tool.execute({"arguments": {}})

    assert result.ok is False
    assert result.error_code == "WRITE_FORBIDDEN"
    assert "MCP tool 未授权" in result.output


def test_mcp_executor_reports_schema_and_timeout_failures_as_recoverable_tool_errors():
    executor = InMemoryMcpExecutor()
    executor.register("browser", "screenshot", lambda args: (_ for _ in ()).throw(TimeoutError("slow")))
    tool = McpTool(
        descriptor=McpToolDescriptor(server="browser", name="screenshot", description="Screenshot"),
        executor=executor,
    )

    invalid = tool.execute({"arguments": "not-object"})
    timeout = tool.execute(McpExecutionRequest(arguments={}).to_payload())

    assert invalid.ok is False
    assert invalid.error_code == "TOOL_INVALID_ARGUMENTS"
    assert timeout.ok is False
    assert timeout.error_code == "TOOL_TIMEOUT"
    assert timeout.result_envelope["schema_version"] == "capability_failure.v1"
    assert timeout.result_envelope["recommended_action"] == "retry_with_smaller_scope_or_longer_timeout"
    assert "capability_search" in timeout.result_envelope["fallback_actions"]
