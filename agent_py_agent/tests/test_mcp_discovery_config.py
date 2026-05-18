from __future__ import annotations

from agent_py_agent.agent.capability.mcp import McpToolDescriptor
from agent_py_agent.agent.capability.mcp_config import _merge_discovered_descriptors
from agent_py_agent.agent.capability.mcp_runtime import McpStdioServerSpec, McpTool


def test_mcp_discovery_keeps_explicit_descriptor_and_skips_failing_server() -> None:
    executor = _FakeDiscoveryExecutor(
        {
            "demo": [
                {
                    "name": "echo",
                    "description": "Discovered echo",
                    "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}},
                }
            ],
            "broken": RuntimeError("offline"),
        }
    )
    explicit = McpToolDescriptor(server="demo", name="echo", description="Explicit echo", source="manual")

    descriptors = _merge_discovered_descriptors(
        [explicit],
        executor,
        [
            McpStdioServerSpec(name="demo", command=["demo"]),
            McpStdioServerSpec(name="broken", command=["broken"]),
        ],
    )

    assert [(item.server, item.name, item.description, item.source) for item in descriptors] == [
        ("demo", "echo", "Explicit echo", "manual")
    ]
    assert executor.calls == ["demo", "broken"]


def test_mcp_schema_details_include_required_enum_description_and_nested_object() -> None:
    tool = McpTool(
        descriptor=McpToolDescriptor(
            server="shop",
            name="search",
            description="Search catalog",
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Search text"},
                    "mode": {"type": "string", "enum": ["cheap", "fast"]},
                    "filters": {
                        "type": "object",
                        "properties": {"brand": {"type": "string"}, "max_price": {"type": "number"}},
                    },
                },
            },
        ),
        executor=_FakeDiscoveryExecutor({}),
    )

    details = tool.spec.parameter_details

    assert details["query"] == "string; required; Search text"
    assert details["mode"] == "string; enum=cheap|fast"
    assert details["filters"] == "object{brand:string, max_price:number}"


class _FakeDiscoveryExecutor:
    def __init__(self, tools_by_server):
        self.tools_by_server = tools_by_server
        self.calls: list[str] = []

    def list_tools(self, server: str):
        self.calls.append(server)
        result = self.tools_by_server.get(server, [])
        if isinstance(result, Exception):
            raise result
        return result

    def execute(self, server: str, name: str, arguments: dict):
        return {"server": server, "name": name, "arguments": arguments}
