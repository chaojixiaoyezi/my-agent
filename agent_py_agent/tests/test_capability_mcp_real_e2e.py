from __future__ import annotations

"""LLM: real-process MCP capability tests for registry and agent tool-loop integration.

给人看的解释：
这些测试不只调用内存 fake handler，而是启动一个本地 Python 子进程，按 MCP stdio
JSON-RPC 协议完成 initialize 和 tools/call，验证真实进程边界可以贯通。
"""

import json
import sys
from pathlib import Path

from agent_py_agent.agent.backend import BaseBackend, ModelResponse
from agent_py_agent.agent.capability.grants import CapabilityGrantScope
from agent_py_agent.agent.capability.mcp import McpToolDescriptor
from agent_py_agent.agent.capability.mcp_runtime import McpStdioServerSpec, StdioMcpExecutor
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

_DEMO_MCP_SERVER_SOURCE = r'''
import json
import sys


def send(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demo", "version": "test"},
            },
        })
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [{
                    "name": "echo",
                    "description": "Echo through a real MCP stdio server",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                    },
                }]
            },
        })
    elif method == "tools/call":
        arguments = message.get("params", {}).get("arguments", {})
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": "真实 MCP pong: " + str(arguments.get("message", ""))}],
                "isError": False,
            },
        })
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}})
'''.lstrip()


# LLM: _McpToolLoopBackend simulates model choices while leaving MCP execution real.
# 类用途: 测试专用后端，按模型轮次先查目录、再调用 MCP、最后根据工具结果收口。
class _McpToolLoopBackend(BaseBackend):
    name = "mcp_tool_loop_backend"

    # LLM: _McpToolLoopBackend.__init__ tracks model prompts for end-to-end assertions.
    # 函数用途: 初始化调用计数和 prompt 记录，验证工具结果回灌到下一轮模型输入。
    def __init__(self):
        self.calls = 0
        self.prompts: list[str] = []

    # LLM: _McpToolLoopBackend.generate emits realistic text-protocol tool calls.
    # 函数用途: 模拟模型主动发现 capability、调用 MCP tool 并根据真实结果给最终回答。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            assert "capability_search" in prompt
            return ModelResponse(
                text=_tool_call({"tool": "capability_search", "query": "demo echo mcp", "limit": 5}),
                backend=self.name,
            )
        if self.calls == 2:
            assert "mcp:demo:echo" in prompt
            return ModelResponse(
                text=_tool_call({"tool": "mcp.demo.echo", "arguments": {"message": "真实 MCP ping"}}),
                backend=self.name,
            )
        assert "真实 MCP pong: 真实 MCP ping" in prompt
        return ModelResponse(text="真实 MCP 链路已完成。", backend=self.name)


def test_stdio_mcp_executor_calls_real_json_rpc_server(tmp_path: Path) -> None:
    server = _write_demo_mcp_server(tmp_path)
    executor = StdioMcpExecutor(
        [McpStdioServerSpec(name="demo", command=[sys.executable, "-u", str(server)])]
    )
    registry = _registry(tmp_path, executor)

    try:
        result = registry.execute_call(
            {"tool": "mcp.demo.echo", "arguments": {"message": "hello"}},
            allowed_tools=["mcp.demo.echo"],
        )
    finally:
        registry.close()

    payload = json.loads(result.output)
    assert result.ok is True
    assert payload["result"]["content"][0]["text"] == "真实 MCP pong: hello"


def test_simple_agent_uses_configured_real_stdio_mcp_server_end_to_end(tmp_path: Path) -> None:
    server = _write_demo_mcp_server(tmp_path)
    config = AgentConfig(
        model_backend="echo",
        max_tool_rounds=4,
        tool_vector_search_enabled=False,
        mcp_stdio_servers=[
            {
                "name": "demo",
                "command": [sys.executable, "-u", str(server)],
                "request_timeout_seconds": 5,
            }
        ],
        mcp_tool_descriptors=[
            {
                "server": "demo",
                "name": "echo",
                "description": "Echo through a real MCP stdio server",
                "capabilities": ["mcp", "echo"],
                "keywords": ["demo", "echo"],
                "risk_level": "low",
            }
        ],
        capability_grant_mcp_tools=["demo:echo"],
    )
    agent = SimpleAgent(config, tmp_path)
    agent.backend = _McpToolLoopBackend()

    try:
        result = agent.run("请先找可用 MCP echo 能力，然后用真实 MCP server 回显。", save=False)
    finally:
        agent.tools.close()

    assert result.response == "真实 MCP 链路已完成。"
    assert result.executed_tools == ["capability_search", "mcp.demo.echo"]
    assert result.tool_rounds == 2


def test_simple_agent_auto_discovers_mcp_tools_list_schema(tmp_path: Path) -> None:
    server = _write_demo_mcp_server(tmp_path)
    config = AgentConfig(
        model_backend="echo",
        max_tool_rounds=1,
        tool_vector_search_enabled=False,
        mcp_auto_discover_tools=True,
        mcp_stdio_servers=[
            {
                "name": "demo",
                "command": [sys.executable, "-u", str(server)],
                "request_timeout_seconds": 5,
            }
        ],
        capability_grant_mcp_tools=["demo:echo"],
    )
    agent = SimpleAgent(config, tmp_path)

    try:
        specs = {spec.name: spec for spec in agent.tools.specs(include_orchestration=True)}
        detail = agent.tools.execute_call({"tool": "capability_describe", "id": "mcp:demo:echo"})
    finally:
        agent.tools.close()

    payload = json.loads(detail.output)
    assert "mcp.demo.echo" in specs
    assert specs["mcp.demo.echo"].parameter_details["message"] == "string"
    assert payload["detail"]["metadata"]["input_schema"]["properties"]["message"]["type"] == "string"


# LLM: _registry builds a focused registry with one real-process MCP tool.
# 函数用途: 构造测试用 ToolRegistry，注入 stdio executor 和授权范围。
def _registry(root: Path, executor: StdioMcpExecutor) -> ToolRegistry:
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
            mcp_tools=[
                McpToolDescriptor(
                    server="demo",
                    name="echo",
                    description="Echo through a real MCP stdio server",
                    capabilities=["mcp", "echo"],
                    keywords=["demo"],
                    risk_level="low",
                )
            ],
            mcp_executor=executor,
            capability_grant_scope=CapabilityGrantScope(mcp_tools=["demo:echo"]),
        )
    )


# LLM: _tool_call renders one JSON payload in the runtime's text tool protocol.
# 函数用途: 把测试后端的结构化工具调用包装成模型会输出的 TOOL_CALL 文本。
def _tool_call(payload: dict[str, object]) -> str:
    return "[TOOL_CALL]\n" + json.dumps(payload, ensure_ascii=False) + "\n[/TOOL_CALL]"


# LLM: _write_demo_mcp_server writes a minimal real MCP stdio JSON-RPC server.
# 函数用途: 生成测试子进程脚本，支持 initialize、initialized notification 和 tools/call。
def _write_demo_mcp_server(tmp_path: Path) -> Path:
    server = tmp_path / "demo_mcp_server.py"
    server.write_text(_DEMO_MCP_SERVER_SOURCE, encoding="utf-8")
    return server
