from __future__ import annotations

"""自包含 MCP 客户端测试：测试内含一个极简 stdio MCP server 脚本（实现 initialize/
tools/list/tools/call 的 JSON-RPC over stdio），不依赖任何外部 MCP server。

覆盖：
- initialize 握手 → tools/list 发现工具 → tools/call 调用 → 拿到结果（端到端真跑子进程）。
- server 起不来 / 调用超时 / 协议错（JSON-RPC error）→ 优雅结构化报错，不崩。
- 工具名前缀 mcp__<server>__<tool> 防冲突 + server 撞名跳过。
- MCP inputSchema → my-agent parameter_schema/parameters/required 转换。
- 凭证脱敏 + env 隔离 + 日志脱敏。
- 惰性：mcp_servers 为空时不起任何子进程。
- 经 ToolRegistry 端到端动态注册 + 模型侧调用转发。
"""

import json
import sys
import textwrap

import pytest

from agent_py_agent.agent.tooling.mcp_client import (
    MCPError,
    MCPServerConfig,
    MCPStdioClient,
    build_safe_env,
    redact_env_for_log,
    sanitize_credentials,
)
from agent_py_agent.agent.tooling.mcp_registration import (
    build_proxy_tool,
    input_schema_to_parameters,
    mcp_tool_name,
    parse_mcp_servers,
    register_mcp_servers,
    sanitize_name_component,
)
from agent_py_agent.agent.tooling.mcp_client import MCPToolInfo

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 自包含 stdio MCP server 脚本（实现最小 JSON-RPC over stdio：换行分隔 JSON）。
# 暴露两个工具：echo(text) 和 add(a, b)。
# ---------------------------------------------------------------------------

_ECHO_SERVER = textwrap.dedent(
    '''
    import json, sys

    TOOLS = [
        {
            "name": "echo",
            "description": "Echo back the provided text",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "text to echo"}},
                "required": ["text"],
            },
        },
        {
            "name": "add",
            "description": "Add two integers",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "a": {"type": "integer", "description": "first addend"},
                    "b": {"type": "integer", "description": "second addend"},
                },
                "required": ["a", "b"],
            },
        },
    ]

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\\n")
        sys.stdout.flush()

    def main():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            method = req.get("method")
            rid = req.get("id")
            if method == "initialize":
                send({"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "echo-server", "version": "0.1"},
                }})
            elif method == "notifications/initialized":
                pass  # notification, no response
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                params = req.get("params") or {}
                name = params.get("name")
                args = params.get("arguments") or {}
                if name == "echo":
                    text = str(args.get("text", ""))
                    send({"jsonrpc": "2.0", "id": rid, "result": {
                        "content": [{"type": "text", "text": text}], "isError": False,
                    }})
                elif name == "add":
                    total = int(args.get("a", 0)) + int(args.get("b", 0))
                    send({"jsonrpc": "2.0", "id": rid, "result": {
                        "content": [{"type": "text", "text": str(total)}], "isError": False,
                    }})
                else:
                    send({"jsonrpc": "2.0", "id": rid, "error": {
                        "code": -32601, "message": "unknown tool " + str(name),
                    }})
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {
                    "code": -32601, "message": "method not found",
                }})

    main()
    '''
)

# server 故意对每个 tools/call 不回包（用于测 client 调用超时）。
_HANG_ON_CALL_SERVER = textwrap.dedent(
    '''
    import json, sys

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {}, "serverInfo": {}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "slow", "description": "never returns", "inputSchema": {}}
            ]}})
        # tools/call 与 notifications 一律不回包 → 触发 client 超时。
    '''
)

# server 启动即崩（用于测 initialize 握手前进程死亡）。
_CRASH_SERVER = "import sys; sys.exit(1)"


def _config(script: str, name: str = "echo", **overrides) -> MCPServerConfig:
    kwargs = {
        "name": name,
        "command": sys.executable,
        "args": ["-c", script],
        "connect_timeout": 10.0,
        "timeout": 10.0,
    }
    kwargs.update(overrides)
    return MCPServerConfig(**kwargs)


# ---------------------------------------------------------------------------
# 端到端：握手 → 发现 → 调用
# ---------------------------------------------------------------------------

def test_handshake_discover_and_call_end_to_end():
    client = MCPStdioClient(_config(_ECHO_SERVER))
    try:
        client.start()
        # initialize 握手回包被捕获
        assert client.server_info.get("name") == "echo-server"

        # tools/list 发现两个工具
        tools = client.list_tools()
        names = sorted(t.name for t in tools)
        assert names == ["add", "echo"]

        # tools/call: echo
        echo_result = client.call_tool("echo", {"text": "hello mcp"})
        assert echo_result["isError"] is False
        assert echo_result["content"] == "hello mcp"

        # tools/call: add（验证非 string 参数透传）
        add_result = client.call_tool("add", {"a": 2, "b": 40})
        assert add_result["content"] == "42"
    finally:
        client.stop()


def test_call_unknown_tool_surfaces_protocol_error():
    client = MCPStdioClient(_config(_ECHO_SERVER))
    try:
        client.start()
        with pytest.raises(MCPError) as exc_info:
            client.call_tool("does_not_exist", {})
        assert exc_info.value.code == "MCP_PROTOCOL_ERROR"
    finally:
        client.stop()


# ---------------------------------------------------------------------------
# 异常兜底：起不来 / 超时 / 进程崩
# ---------------------------------------------------------------------------

def test_server_command_not_found_raises_structured_error():
    config = MCPServerConfig(name="missing", command="this_command_does_not_exist_xyz", args=[])
    client = MCPStdioClient(config)
    with pytest.raises(MCPError) as exc_info:
        client.start()
    assert exc_info.value.code == "MCP_SERVER_START_FAILED"
    # 错误信息可读、含 server 名
    assert "missing" in str(exc_info.value)


def test_server_crash_before_handshake_raises_connection_closed():
    client = MCPStdioClient(_config(_CRASH_SERVER, name="crasher", connect_timeout=5.0))
    with pytest.raises(MCPError) as exc_info:
        client.start()
    # 进程在 initialize 回包前退出 → 连接关闭错误（而非 hang）
    assert exc_info.value.code in {"MCP_CONNECTION_CLOSED", "MCP_TIMEOUT"}


def test_tool_call_timeout_is_structured_and_not_hang():
    client = MCPStdioClient(_config(_HANG_ON_CALL_SERVER, name="slow", timeout=1.0))
    try:
        client.start()
        tools = client.list_tools()
        assert [t.name for t in tools] == ["slow"]
        with pytest.raises(MCPError) as exc_info:
            client.call_tool("slow", {})
        assert exc_info.value.code == "MCP_TIMEOUT"
    finally:
        client.stop()


def test_call_after_stop_raises_connection_closed():
    client = MCPStdioClient(_config(_ECHO_SERVER))
    client.start()
    client.stop()
    with pytest.raises(MCPError) as exc_info:
        client.call_tool("echo", {"text": "x"})
    assert exc_info.value.code == "MCP_CONNECTION_CLOSED"


def test_stop_is_idempotent():
    client = MCPStdioClient(_config(_ECHO_SERVER))
    client.start()
    client.stop()
    client.stop()  # 再次调用不应抛


# ---------------------------------------------------------------------------
# 配置健壮性
# ---------------------------------------------------------------------------

def test_config_missing_command_rejected():
    with pytest.raises(MCPError) as exc_info:
        MCPServerConfig.from_mapping("bad", {"args": ["x"]})
    assert exc_info.value.code == "MCP_CONFIG_INVALID"


def test_config_non_mapping_rejected():
    with pytest.raises(MCPError):
        MCPServerConfig.from_mapping("bad", ["not", "a", "dict"])


def test_config_invalid_timeout_falls_back_to_default():
    config = MCPServerConfig.from_mapping(
        "demo", {"command": "x", "timeout": "not-a-number", "connect_timeout": -5}
    )
    assert config.timeout == 60.0  # _DEFAULT_TOOL_TIMEOUT
    assert config.connect_timeout == 30.0  # _DEFAULT_CONNECT_TIMEOUT


def test_parse_mcp_servers_skips_invalid_entries():
    configs = parse_mcp_servers(
        {
            "good": {"command": "echo"},
            "bad": {"args": ["no command"]},  # 缺 command → 跳过
        }
    )
    assert [c.name for c in configs] == ["good"]


def test_parse_mcp_servers_empty_returns_empty():
    assert parse_mcp_servers({}) == []
    assert parse_mcp_servers(None) == []
    assert parse_mcp_servers("not a dict") == []
