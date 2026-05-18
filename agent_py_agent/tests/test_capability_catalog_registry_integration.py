from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.capability.grants import CapabilityGrantScope
from agent_py_agent.agent.capability.mcp import McpToolDescriptor
from agent_py_agent.agent.capability.mcp_runtime import InMemoryMcpExecutor
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams


def _registry(
    root: Path,
    *,
    expose_security_tools: bool = False,
    mcp_tools: list[McpToolDescriptor] | None = None,
    mcp_executor: InMemoryMcpExecutor | None = None,
    grant_scope: CapabilityGrantScope | None = None,
) -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=root,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=5,
            vector_search_enabled=False,
            shell_tool_timeout=30,
            expose_security_tools=expose_security_tools,
            mcp_tools=mcp_tools or [],
            mcp_executor=mcp_executor,
            capability_grant_scope=grant_scope,
        )
    )


def test_tool_registry_registers_capability_catalog_tools(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    specs = {spec.name: spec for spec in registry.specs(include_orchestration=True)}

    assert specs["capability_search"].category == "capability"
    assert specs["capability_describe"].category == "capability"


def test_capability_search_tool_uses_visible_registry_specs(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    result = registry.execute_call({"tool": "capability_search", "query": "read file", "limit": 5})
    payload = json.loads(result.output)
    names = [item["name"] for item in payload["results"]]

    assert result.ok is True
    assert "read_file" in names
    assert "security_query" not in names


def test_capability_describe_tool_does_not_describe_hidden_security_tools(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    result = registry.execute_call({"tool": "capability_describe", "id": "tool:security_query"})
    payload = json.loads(result.output)

    assert result.ok is False
    assert payload["error"]["code"] == "CAPABILITY_NOT_FOUND"


def test_capability_catalog_can_include_security_tools_when_explicitly_exposed(tmp_path: Path) -> None:
    registry = _registry(tmp_path, expose_security_tools=True)

    result = registry.execute_call({"tool": "capability_search", "query": "security query", "limit": 10})
    payload = json.loads(result.output)

    assert result.ok is True
    assert "security_query" in [item["name"] for item in payload["results"]]


def test_tool_registry_registers_and_executes_mcp_tools_with_scope(tmp_path: Path) -> None:
    executor = InMemoryMcpExecutor()
    executor.register("browser", "screenshot", lambda args: {"path": args["path"]})
    registry = _registry(
        tmp_path,
        mcp_tools=[
            McpToolDescriptor(
                server="browser",
                name="screenshot",
                description="Capture screenshots",
                capabilities=["browser", "screenshot"],
                keywords=["ui"],
            )
        ],
        mcp_executor=executor,
        grant_scope=CapabilityGrantScope(mcp_tools=["browser:screenshot"]),
    )

    specs = {spec.name: spec for spec in registry.specs(include_orchestration=True)}
    result = registry.execute_call(
        {"tool": "mcp.browser.screenshot", "arguments": {"path": "shot.png"}},
        allowed_tools=["mcp.browser.screenshot"],
    )
    payload = json.loads(result.output)
    search = registry.execute_call({"tool": "capability_search", "query": "browser screenshot", "limit": 5})
    search_payload = json.loads(search.output)

    assert "mcp.browser.screenshot" in specs
    assert result.ok is True
    assert payload["result"] == {"path": "shot.png"}
    assert "screenshot" in [item["name"] for item in search_payload["results"]]


def test_tool_registry_hides_mcp_tools_outside_scope(tmp_path: Path) -> None:
    executor = InMemoryMcpExecutor()
    registry = _registry(
        tmp_path,
        mcp_tools=[McpToolDescriptor(server="browser", name="screenshot", description="Capture screenshots")],
        mcp_executor=executor,
        grant_scope=CapabilityGrantScope(tools=["read_file"]),
    )

    specs = {spec.name: spec for spec in registry.specs(include_orchestration=True)}
    search = registry.execute_call({"tool": "capability_search", "query": "browser screenshot", "limit": 5})
    search_payload = json.loads(search.output)

    assert "mcp.browser.screenshot" not in specs
    assert all(item["id"] != "mcp:browser:screenshot" for item in search_payload["results"])
