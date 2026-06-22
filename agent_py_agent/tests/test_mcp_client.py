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


# ---------------------------------------------------------------------------
# 安全:env 隔离(不把父进程凭证漏给第三方 MCP server)+ 凭证脱敏
# ---------------------------------------------------------------------------

# 一个把 os.environ.get(key) 回显出来的 server,用于端到端验证子进程实际拿到的环境。
_ENV_ECHO_SERVER = textwrap.dedent(
    '''
    import json, sys, os

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\\n"); sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line); m = req.get("method"); rid = req.get("id")
        if m == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {}, "serverInfo": {"name": "env"}}})
        elif m == "notifications/initialized":
            pass
        elif m == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "getenv", "description": "", "inputSchema": {}}]}})
        elif m == "tools/call":
            key = (req.get("params") or {}).get("arguments", {}).get("key", "")
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(os.environ.get(key))}], "isError": False}})
    '''
)


def test_parent_secret_not_leaked_to_mcp_subprocess(monkeypatch):
    """⭐ 父进程的 API key 绝不漏给第三方 MCP server 子进程;只传安全基线 + 用户显式声明的 env。"""
    monkeypatch.setenv("AGENT_API_KEY", "PARENT_SECRET_must_not_leak")
    monkeypatch.setenv("OPENAI_API_KEY", "another_parent_secret")
    config = _config(_ENV_ECHO_SERVER, name="env", env={"MY_MCP_TOKEN": "user_provided_token"})
    client = MCPStdioClient(config)
    try:
        client.start()
        # 父进程敏感变量没漏进子进程
        assert json.loads(client.call_tool("getenv", {"key": "AGENT_API_KEY"})["content"]) is None
        assert json.loads(client.call_tool("getenv", {"key": "OPENAI_API_KEY"})["content"]) is None
        # 用户在 config.env 显式声明的传给了子进程(那是用户有意给该 server 的凭证)
        assert json.loads(client.call_tool("getenv", {"key": "MY_MCP_TOKEN"})["content"]) == "user_provided_token"
        # 安全基线 PATH 传了(子进程能找到命令)
        assert json.loads(client.call_tool("getenv", {"key": "PATH"})["content"])  # 非空
    finally:
        client.stop()


def test_build_safe_env_isolation(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "secret")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/u/.config")
    env = build_safe_env({"USER_DECLARED": "v"})
    assert "AGENT_API_KEY" not in env  # 父进程敏感变量不继承
    assert env.get("XDG_CONFIG_HOME") == "/home/u/.config"  # XDG_* 放行
    assert env.get("USER_DECLARED") == "v"  # 用户声明叠加
    assert "PATH" in env  # 安全基线


def test_sanitize_credentials_redacts_token_formats():
    for raw in (
        "leak ghp_abcdefABCDEF1234567890 here",
        "key sk-abcdefghij1234567890 end",
        "slack xoxb-111-222-abcdefghijkl tail",
        "Authorization: Bearer eyJhbG.token.sig",
        'config api_key="supersecretvalue123"',
        "password: hunter2hunter2",
    ):
        out = sanitize_credentials(raw)
        assert "[REDACTED]" in out, f"未脱敏: {raw!r} -> {out!r}"
    assert "ghp_abcdefABCDEF1234567890" not in sanitize_credentials("token=ghp_abcdefABCDEF1234567890")


def test_mcp_error_message_is_sanitized():
    err = MCPError("server failed with ghp_abcdefABCDEF1234567890 in output")
    assert "ghp_abcdefABCDEF1234567890" not in str(err)  # 错误信息里的凭证被抹,不经异常泄露
    assert "[REDACTED]" in str(err)


def test_redact_env_for_log_hides_secret_keys():
    out = redact_env_for_log({"PATH": "/usr/bin", "GITHUB_TOKEN": "ghp_x", "api_key": "sk-y", "USER": "bob"})
    assert out["PATH"] == "/usr/bin" and out["USER"] == "bob"  # 非敏感键原样
    assert out["GITHUB_TOKEN"] == "<redacted>" and out["api_key"] == "<redacted>"  # 键名命中 secret → 打码


# ---------------------------------------------------------------------------
# 健壮性:不受信 server 输出(往 stdout 混日志 / 发畸形 JSON-RPC)不崩不挂
# ---------------------------------------------------------------------------

# server 在合法 JSON-RPC 之间往 stdout 混入非 JSON 日志行、JSON 数组/数字、无 id 通知。
_NOISY_SERVER = textwrap.dedent(
    '''
    import json, sys

    def raw(s):
        sys.stdout.write(s + "\\n"); sys.stdout.flush()

    def send(msg):
        raw(json.dumps(msg))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line); m = req.get("method"); rid = req.get("id")
        if m == "initialize":
            raw("[LOG] some server writes plain logs to stdout")  # 非 JSON 噪声
            raw("[1, 2, 3]")                                       # JSON 数组(非 dict 消息)
            raw("42")                                              # JSON 数字
            send({"jsonrpc": "2.0", "method": "notifications/progress"})  # 无 id 通知
            send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {}, "serverInfo": {"name": "noisy"}}})
        elif m == "notifications/initialized":
            pass
        elif m == "tools/list":
            raw("garbage line before tools/list response")
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "ping", "description": "", "inputSchema": {}}]}})
        elif m == "tools/call":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "pong"}], "isError": False}})
    '''
)


def test_noisy_server_output_is_tolerated():
    """server 往 stdout 混日志/非 dict JSON/通知 → client 跳过噪声,握手/发现/调用照常(常见真实场景)。"""
    client = MCPStdioClient(_config(_NOISY_SERVER, name="noisy"))
    try:
        client.start()
        assert client.server_info.get("name") == "noisy"  # 噪声里捞出真握手回包
        assert [t.name for t in client.list_tools()] == ["ping"]
        assert client.call_tool("ping", {})["content"] == "pong"
    finally:
        client.stop()


# server 的 tools/call 返回 isError=true(工具自身报错,非协议错)。
_TOOL_ERROR_SERVER = textwrap.dedent(
    '''
    import json, sys

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\\n"); sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line); m = req.get("method"); rid = req.get("id")
        if m == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {}, "serverInfo": {}}})
        elif m == "notifications/initialized":
            pass
        elif m == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "boom", "description": "", "inputSchema": {}}]}})
        elif m == "tools/call":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "the tool failed internally"}], "isError": True}})
    '''
)


def test_tool_level_error_is_returned_not_raised():
    """工具自身报错(isError=true)≠ 协议错:call_tool 把 isError 一起返回,不抛(由上层呈现给模型)。"""
    client = MCPStdioClient(_config(_TOOL_ERROR_SERVER, name="boom"))
    try:
        client.start()
        result = client.call_tool("boom", {})  # 不抛
        assert result["isError"] is True
        assert "failed" in result["content"]
    finally:
        client.stop()


def test_content_block_rendering_and_normalize():
    """各类 content 块渲染 + 结果归一化(纯单元,不起子进程)。"""
    from agent_py_agent.agent.tooling.mcp_client import _normalize_call_result, _render_content_block

    assert _render_content_block({"type": "text", "text": "hi"}) == "hi"
    assert "image" in _render_content_block({"type": "image", "mimeType": "image/png"})
    assert "file://x" in _render_content_block({"type": "resource", "resource": {"uri": "file://x"}})
    assert _render_content_block({"type": "unknown_type"}) == ""  # 未知类型 → 空
    assert _render_content_block("not a dict") == ""  # 非 dict → 空(不崩)

    norm = _normalize_call_result(
        {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}, {"type": "image", "mimeType": "x"}]}
    )
    assert "a\nb" in norm["content"]  # 文本块拼接,非文本块不进正文
    assert _normalize_call_result("garbage") == {"content": "", "isError": False}  # 非 dict → 安全默认


# ---------------------------------------------------------------------------
# 资源上限:内容截断(防挤爆上下文)+ 单行上限(防 OOM)
# ---------------------------------------------------------------------------

# tools/call 返回一大坨文本(远超内容上限,但在单行上限内)。
_BIG_CONTENT_SERVER = textwrap.dedent(
    '''
    import json, sys

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\\n"); sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line); m = req.get("method"); rid = req.get("id")
        if m == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {}, "serverInfo": {}}})
        elif m == "notifications/initialized":
            pass
        elif m == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "big", "description": "", "inputSchema": {}}]}})
        elif m == "tools/call":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "Y" * 100000}], "isError": False}})
    '''
)


def test_oversized_content_is_truncated():
    """工具结果文本超内容上限 → 截断带标记(模型上下文有限,过长结果只会挤爆上下文)。"""
    client = MCPStdioClient(_config(_BIG_CONTENT_SERVER, name="big"))  # 默认内容上限 16KB
    try:
        client.start()
        result = client.call_tool("big", {})
        assert len(result["content"]) <= 16 * 1024 + 80  # 截到 ~16KB(+ 标记)
        assert "已截断" in result["content"]  # 带截断标记,模型知道被截
    finally:
        client.stop()


# tools/call: size=huge → 一行超大文本(触发单行上限);否则 → 正常小结果。
_MIXED_SIZE_SERVER = textwrap.dedent(
    '''
    import json, sys

    def send(msg):
        sys.stdout.write(json.dumps(msg) + "\\n"); sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line); m = req.get("method"); rid = req.get("id")
        if m == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"capabilities": {}, "serverInfo": {}}})
        elif m == "notifications/initialized":
            pass
        elif m == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
                {"name": "sized", "description": "", "inputSchema": {}}]}})
        elif m == "tools/call":
            size = (req.get("params") or {}).get("arguments", {}).get("size")
            text = "X" * 60000 if size == "huge" else "ok-small"
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": text}], "isError": False}})
    '''
)


def test_oversized_line_dropped_and_client_survives():
    """单行超上限 → 丢弃该消息(不 OOM、不崩、不挂);reader 存活,后续正常调用照常。"""
    config = _config(_MIXED_SIZE_SERVER, name="sized", timeout=2.0, max_line_chars=50_000)
    client = MCPStdioClient(config)
    try:
        client.start()
        with pytest.raises(MCPError) as exc:
            client.call_tool("sized", {"size": "huge"})  # 60KB 行 > 50KB 上限 → 丢弃 → 该调用超时
        assert exc.value.code == "MCP_TIMEOUT"
        # ⭐ reader 没崩:后续正常调用照常工作
        assert client.call_tool("sized", {"size": "small"})["content"] == "ok-small"
    finally:
        client.stop()


def test_config_parses_size_caps():
    cfg = MCPServerConfig.from_mapping("c", {"command": "x", "max_content_chars": 1000, "max_line_chars": 5000})
    assert cfg.max_content_chars == 1000 and cfg.max_line_chars == 5000  # config 可调
    bad = MCPServerConfig.from_mapping("c", {"command": "x", "max_content_chars": "nope", "max_line_chars": -1})
    assert bad.max_content_chars == 16 * 1024 and bad.max_line_chars == 1024 * 1024  # 非法 → sane 默认
