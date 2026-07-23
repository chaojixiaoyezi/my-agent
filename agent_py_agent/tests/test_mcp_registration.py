from __future__ import annotations

"""MCP 工具注册 / schema 转换 / 凭证脱敏 / 端到端经 ToolRegistry 注册的测试。

复用 test_mcp_client 里的自包含 echo MCP server 脚本（不依赖外部 server）。
"""

import json
import sys

import pytest

from agent_py_agent.agent.tooling.mcp_client import (
    MCPError,
    MCPServerConfig,
    MCPStdioClient,
    MCPToolInfo,
    build_safe_env,
    redact_env_for_log,
    sanitize_credentials,
)
from agent_py_agent.agent.tooling.mcp_registration import (
    MCPProxyTool,
    build_proxy_tool,
    input_schema_to_parameters,
    mcp_tool_name,
    register_mcp_servers,
    sanitize_name_component,
)
from agent_py_agent.tests.test_mcp_client import _ECHO_SERVER

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 工具名前缀 / 防冲突
# ---------------------------------------------------------------------------

def test_mcp_tool_name_has_prefix_and_sanitizes():
    assert mcp_tool_name("github", "create_issue") == "mcp__github__create_issue"
    # 大写 / 空格 / 连字符 / 特殊字符都被清洗成 [a-z0-9_]
    assert mcp_tool_name("GitHub Server", "create-issue!") == "mcp__github_server__create_issue"


def test_sanitize_name_component_never_empty():
    assert sanitize_name_component("") == "x"
    assert sanitize_name_component("@@@") == "x"
    assert sanitize_name_component("OK_1") == "ok_1"


def test_mcp_tool_names_do_not_collide_with_builtin():
    # mcp__ 前缀确保不会撞上 read_file/write_file/run_command 等内置名
    name = mcp_tool_name("filesystem", "read_file")
    assert name == "mcp__filesystem__read_file"
    assert name != "read_file"


# ---------------------------------------------------------------------------
# inputSchema → parameter_schema 转换
# ---------------------------------------------------------------------------

def test_input_schema_conversion_preserves_precise_types_and_required():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "integer", "description": "first"},
            "b": {"type": "string"},
            "mode": {"type": "string", "enum": ["x", "y"], "description": "pick"},
            "items": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["a", "mode"],
    }
    parameters, parameter_schema, required = input_schema_to_parameters(schema)

    # parameters: {名: 描述}
    assert parameters["a"] == "first"
    assert parameters["b"] == ""
    # parameter_schema: 精确类型片段透传（type/enum/items）
    assert parameter_schema["a"] == {"type": "integer"}
    assert parameter_schema["mode"] == {"type": "string", "enum": ["x", "y"]}
    assert parameter_schema["items"] == {"type": "array", "items": {"type": "string"}}
    # required 仅保留真实存在的参数
    assert sorted(required) == ["a", "mode"]


def test_input_schema_conversion_handles_missing_or_empty_schema():
    assert input_schema_to_parameters({}) == ({}, {}, [])
    assert input_schema_to_parameters(None) == ({}, {}, [])
    # 无 properties 时返回空（工具仍可注册，调用时透传任意 arguments）
    assert input_schema_to_parameters({"type": "object"}) == ({}, {}, [])


def test_build_proxy_tool_spec_matches_native_tool_use_contract():
    info = MCPToolInfo(
        name="add",
        description="Add two integers",
        input_schema={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    )
    proxy = build_proxy_tool(client=None, server_name="calc", info=info)  # type: ignore[arg-type]
    spec = proxy.spec
    assert spec.name == "mcp__calc__add"
    assert spec.category == "mcp"
    assert spec.effect == "dangerous"
    assert spec.requires_idempotency is True
    assert spec.requires_approval is True
    assert spec.required_parameters == ["a", "b"]
    assert spec.parameter_schema["a"] == {"type": "integer"}

    # 经 backend schema 转换后是合法的 Anthropic input_schema（对齐 native tool_use）
    from agent_py_agent.agent.backends.tool_schema import tool_spec_to_input_schema

    input_schema = tool_spec_to_input_schema(spec)
    assert input_schema["type"] == "object"
    assert input_schema["properties"]["a"]["type"] == "integer"
    assert sorted(input_schema["required"]) == ["a", "b"]


# ---------------------------------------------------------------------------
# 凭证脱敏 / env 隔离
# ---------------------------------------------------------------------------

def test_sanitize_credentials_redacts_common_secret_shapes():
    assert "ghp_" not in sanitize_credentials("token is ghp_abcdef1234567890")
    assert "sk-" not in sanitize_credentials("key=sk-abcdefghij")
    assert "[REDACTED]" in sanitize_credentials("Authorization: Bearer abc.def.ghi")
    assert "[REDACTED]" in sanitize_credentials('{"api_key": "supersecretvalue"}')
    # 普通文本不被破坏
    assert sanitize_credentials("just normal text") == "just normal text"


def test_redact_env_for_log_masks_secret_keys_only():
    masked = redact_env_for_log({"GITHUB_TOKEN": "ghp_x", "PASSWORD": "p", "REGION": "us"})
    assert masked["GITHUB_TOKEN"] == "<redacted>"
    assert masked["PASSWORD"] == "<redacted>"
    assert masked["REGION"] == "us"


def test_build_safe_env_isolates_parent_secrets(monkeypatch):
    monkeypatch.setenv("SOME_PARENT_SECRET", "leak_me")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = build_safe_env({"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_explicit"})
    # 父进程的任意 secret 不被继承
    assert "SOME_PARENT_SECRET" not in env
    # 安全基线变量被继承
    assert env.get("PATH") == "/usr/bin"
    # 用户显式声明的变量被传入
    assert env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_explicit"


# ---------------------------------------------------------------------------
# 代理工具执行：成功 / server 报错 / 客户端异常
# ---------------------------------------------------------------------------

class _FakeClient:
    """假的 MCPStdioClient，用于单测 MCPProxyTool.execute 的各分支。"""

    def __init__(self, result=None, exc=None, *, running=True):
        self._result = result
        self._exc = exc
        self._running = running
        self.calls: list = []

    def is_running(self):
        return self._running

    def call_tool(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if self._exc is not None:
            raise self._exc
        return self._result


def _proxy(client) -> MCPProxyTool:
    info = MCPToolInfo(name="echo", description="d", input_schema={})
    return build_proxy_tool(client=client, server_name="srv", info=info)


def test_proxy_execute_success_returns_ok_result():
    client = _FakeClient(result={"content": "pong", "isError": False})
    proxy = _proxy(client)
    out = proxy.execute({"tool": "mcp__srv__echo", "text": "ping"})
    assert out.ok is True
    payload = json.loads(out.output)
    assert payload["result"] == "pong"
    # 内部 tool 字段被剥离，只转发真实 arguments
    assert client.calls == [("echo", {"text": "ping"})]


def test_proxy_execute_server_tool_error_maps_to_execution_failed():
    client = _FakeClient(result={"content": "bad input", "isError": True})
    proxy = _proxy(client)
    out = proxy.execute({"x": 1})
    assert out.ok is False
    assert out.error_code == "TOOL_EXECUTION_FAILED"


def test_proxy_execute_timeout_maps_to_tool_timeout():
    client = _FakeClient(exc=MCPError("timed out", code="MCP_TIMEOUT"))
    proxy = _proxy(client)
    out = proxy.execute({})
    assert out.ok is False
    assert out.error_code == "TOOL_TIMEOUT"


def test_proxy_availability_tracks_existing_mcp_process_without_restart():
    client = _FakeClient(running=False)
    proxy = _proxy(client)

    availability = proxy.availability()

    assert availability.available is False
    assert client.calls == []


def test_proxy_execute_redacts_credentials_in_error():
    client = _FakeClient(exc=MCPError("failed with token=ghp_secretleak123", code="MCP_ERROR"))
    proxy = _proxy(client)
    out = proxy.execute({})
    assert "ghp_secretleak123" not in out.output
    assert "[REDACTED]" in out.output


def test_proxy_execute_unexpected_exception_does_not_crash():
    client = _FakeClient(exc=RuntimeError("boom"))
    proxy = _proxy(client)
    out = proxy.execute({})
    assert out.ok is False
    assert out.error_code == "TOOL_EXECUTION_FAILED"


# ---------------------------------------------------------------------------
# 端到端：经 register_mcp_servers 真连 echo server 并注册到一个最小 registry
# ---------------------------------------------------------------------------

class _MiniRegistry:
    """最小 registry 替身：只实现 register + tools 映射（注册逻辑只依赖这两个）。"""

    def __init__(self):
        self.tools: dict = {}

    def register(self, tool):
        self.tools[tool.spec.name] = tool


def _echo_servers_config():
    return {
        "demo": {
            "command": sys.executable,
            "args": ["-c", _ECHO_SERVER],
            "connect_timeout": 10,
            "timeout": 10,
        }
    }


def test_register_mcp_servers_end_to_end_registers_and_calls():
    registry = _MiniRegistry()
    clients = register_mcp_servers(registry, _echo_servers_config())
    try:
        assert len(clients) == 1
        # 两个工具被注册，带 mcp__demo__ 前缀
        assert "mcp__demo__echo" in registry.tools
        assert "mcp__demo__add" in registry.tools
        assert registry.tools["mcp__demo__echo"].spec.effect == "dangerous"

        # 模型侧调用走代理工具 → 转发给 server → 拿到结果
        echo_tool = registry.tools["mcp__demo__echo"]
        out = echo_tool.execute({"tool": "mcp__demo__echo", "text": "roundtrip"})
        assert out.ok is True
        assert json.loads(out.output)["result"] == "roundtrip"

        add_tool = registry.tools["mcp__demo__add"]
        out2 = add_tool.execute({"a": 5, "b": 7})
        assert json.loads(out2.output)["result"] == "12"
    finally:
        for client in clients:
            client.stop()


def test_register_mcp_servers_empty_starts_nothing():
    registry = _MiniRegistry()
    clients = register_mcp_servers(registry, {})
    assert clients == []
    assert registry.tools == {}


def test_register_mcp_servers_skips_unreachable_server():
    registry = _MiniRegistry()
    config = {
        "broken": {"command": "this_command_does_not_exist_xyz", "args": []},
        "good": {"command": sys.executable, "args": ["-c", _ECHO_SERVER], "connect_timeout": 10},
    }
    clients = register_mcp_servers(registry, config)
    try:
        # 坏 server 被跳过，好 server 正常注册（不互相影响）
        assert len(clients) == 1
        assert "mcp__good__echo" in registry.tools
        assert not any(name.startswith("mcp__broken__") for name in registry.tools)
    finally:
        for client in clients:
            client.stop()


def test_register_mcp_servers_name_collision_skips_second():
    registry = _MiniRegistry()
    # 预先占用 mcp__demo__echo，验证撞名跳过（保留先到者）
    sentinel = object()
    registry.tools["mcp__demo__echo"] = sentinel
    clients = register_mcp_servers(registry, _echo_servers_config())
    try:
        # echo 撞名被跳过（仍是 sentinel），add 正常注册
        assert registry.tools["mcp__demo__echo"] is sentinel
        assert "mcp__demo__add" in registry.tools
    finally:
        for client in clients:
            client.stop()


def test_mcp_tool_effect_can_only_be_lowered_by_explicit_server_config():
    registry = _MiniRegistry()
    config = _echo_servers_config()
    config["demo"]["tool_effects"] = {"echo": "read_only", "add": "mutating"}
    clients = register_mcp_servers(registry, config)
    try:
        assert registry.tools["mcp__demo__echo"].spec.effect == "read_only"
        assert registry.tools["mcp__demo__echo"].spec.requires_idempotency is False
        assert registry.tools["mcp__demo__add"].spec.effect == "mutating"
        assert registry.tools["mcp__demo__add"].spec.requires_idempotency is True
        assert registry.tools["mcp__demo__add"].spec.requires_approval is False
    finally:
        for client in clients:
            client.stop()


def test_unknown_mcp_tool_is_blocked_by_runtime_effect_gate_before_call(tmp_path):
    from agent_py_agent.agent.tooling.registry_execution import (
        ExecuteRegistryCallParams,
        execute_registry_call,
    )

    client = _FakeClient(result={"content": "must not run", "isError": False})
    proxy = _proxy(client)
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": proxy.spec.name, "text": "unsafe"},
            tools={proxy.spec.name: proxy},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
        )
    )

    assert result.ok is False
    assert result.error_code == "APPROVAL_REQUIRED"
    assert client.calls == []
