from __future__ import annotations

"""MCP 工具注册 / schema 转换 / 凭证脱敏 / 端到端经 ToolRegistry 注册的测试。

复用 test_mcp_client 里的自包含 echo MCP server 脚本（不依赖外部 server）。
"""

import json
import sys
import textwrap
import threading
from types import SimpleNamespace

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
    mcp_schema_parameters,
    mcp_tool_name,
    register_mcp_servers,
    sanitize_name_component,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    execute_canonical_test_call,
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
# inputSchema → 完整 canonical Schema + 目录投影
# ---------------------------------------------------------------------------

def test_input_schema_catalog_projection_keeps_descriptions_only():
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
    parameters = mcp_schema_parameters(schema)

    # parameters: {名: 描述}
    assert parameters["a"] == "first"
    assert parameters["b"] == ""


def test_input_schema_conversion_handles_missing_or_empty_schema():
    assert mcp_schema_parameters({}) == {}
    assert mcp_schema_parameters(None) == {}
    assert mcp_schema_parameters({"type": "object"}) == {}


def test_build_proxy_tool_contract_matches_native_tool_use_contract():
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
    spec = proxy.model_spec
    policy = proxy.runtime_policy
    assert spec.name == "mcp__calc__add"
    assert spec.category == "mcp"
    assert policy.effect_resolver.default_effect == "dangerous"
    assert policy.output_policy.trust == "external_data"
    assert policy.idempotency_policy.scope == "operation"
    assert policy.approval_policy.mode == "dangerous"
    assert spec.input_schema["properties"]["a"] == {"type": "integer"}

    # 经 backend schema 转换后是合法的 Anthropic input_schema（对齐 native tool_use）
    from agent_py_agent.agent.backends.tool_schema import tool_model_spec_to_input_schema

    input_schema = tool_model_spec_to_input_schema(spec)
    assert input_schema["type"] == "object"
    assert input_schema["properties"]["a"]["type"] == "integer"
    assert sorted(input_schema["required"]) == ["a", "b"]


def test_mcp_full_schema_is_preserved_for_provider_and_runtime():
    schema = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "score": {"type": "number", "minimum": 0, "maximum": 1}
                    },
                    "required": ["score"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["rows"],
        "additionalProperties": False,
    }
    proxy = build_proxy_tool(
        client=None,  # type: ignore[arg-type]
        server_name="calc",
        info=MCPToolInfo(name="rank", description="rank", input_schema=schema),
    )

    from agent_py_agent.agent.backends.tool_schema import tool_model_spec_to_input_schema

    assert proxy.model_spec.input_schema == schema
    assert tool_model_spec_to_input_schema(proxy.model_spec) == schema


def test_mcp_schema_gate_blocks_invalid_arguments_before_remote_call(tmp_path):
    client = _FakeClient(result={"content": "must not run", "isError": False})
    proxy = build_proxy_tool(
        client=client,  # type: ignore[arg-type]
        server_name="calc",
        info=MCPToolInfo(
            name="add",
            description="add",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "integer", "minimum": 1},
                    "b": {"type": "integer", "minimum": 1},
                },
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        ),
        effect="read_only",
    )

    result = execute_canonical_test_call(
        tmp_path,
        tools={proxy.model_spec.name: proxy},
        tool_name=proxy.model_spec.name,
        arguments={"a": 0, "b": 2},
    ).result

    assert result.ok is False
    assert result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert client.calls == []


def test_mcp_schema_gate_rejects_wrong_types_without_remote_call(tmp_path):
    client = _FakeClient(result={"content": "3", "isError": False})
    proxy = build_proxy_tool(
        client=client,  # type: ignore[arg-type]
        server_name="calc",
        info=MCPToolInfo(
            name="add",
            description="add",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        ),
        effect="read_only",
    )

    result = execute_canonical_test_call(
        tmp_path,
        tools={proxy.model_spec.name: proxy},
        tool_name=proxy.model_spec.name,
        arguments={"a": "1", "b": "2"},
    ).result

    assert result.ok is False
    assert result.error_code == "TOOL_PARAMETER_TYPE_INVALID"
    assert result.handler_executed is False
    assert client.calls == []


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
    out = proxy.execute(
        {
            "tool": "mcp__srv__echo",
            "text": "ping",
            "__run_scope": {"run_id": "host-only"},
        }
    )
    assert out.ok is True
    payload = json.loads(out.output)
    assert payload["result"] == "pong"
    # 内部 tool 字段被剥离，只转发真实 arguments
    assert client.calls == [("echo", {"text": "ping"})]


def test_proxy_execute_recursively_redacts_structured_content():
    client = _FakeClient(
        result={
            "content": "ok",
            "isError": False,
            "structuredContent": {
                "api_key": "opaque-secret-value",
                "rows": [{"authorization": "Bearer abc.def.ghi"}, {"count": 2}],
            },
        }
    )

    out = _proxy(client).execute({})

    payload = json.loads(out.output)
    assert payload["structuredContent"]["api_key"] == "<redacted>"
    assert payload["structuredContent"]["rows"][0]["authorization"] == "<redacted>"
    assert payload["structuredContent"]["rows"][1]["count"] == 2


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
        self.tools[tool.model_spec.name] = tool


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
        assert (
            registry.tools["mcp__demo__echo"].runtime_policy.effect_resolver.default_effect
            == "dangerous"
        )

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


def test_register_mcp_servers_retains_unreachable_config_without_exposing_tools():
    registry = _MiniRegistry()
    config = {
        "broken": {"command": "this_command_does_not_exist_xyz", "args": []},
        "good": {"command": sys.executable, "args": ["-c", _ECHO_SERVER], "connect_timeout": 10},
    }
    clients = register_mcp_servers(registry, config)
    try:
        # 坏 server 的配置被保留供后续 run 重连，但不暴露任何工具；好 server 不受影响。
        assert len(clients) == 2
        assert len([client for client in clients if client.is_running()]) == 1
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
        echo_policy = registry.tools["mcp__demo__echo"].runtime_policy
        add_policy = registry.tools["mcp__demo__add"].runtime_policy
        assert echo_policy.effect_resolver.default_effect == "read_only"
        assert echo_policy.idempotency_policy.scope == ""
        assert add_policy.effect_resolver.default_effect == "mutating"
        assert add_policy.idempotency_policy.scope == "operation"
        assert add_policy.approval_policy.mode == "dangerous"
    finally:
        for client in clients:
            client.stop()


def test_unknown_mcp_tool_is_blocked_by_runtime_effect_gate_before_call(tmp_path):
    client = _FakeClient(result={"content": "must not run", "isError": False})
    proxy = _proxy(client)
    result = execute_canonical_test_call(
        tmp_path,
        tools={proxy.model_spec.name: proxy},
        tool_name=proxy.model_spec.name,
        arguments={"text": "unsafe"},
    ).result

    assert result.ok is False
    assert result.error_code == "APPROVAL_REQUIRED"
    assert client.calls == []


def test_registry_run_boundary_reconnects_and_refreshes_dead_mcp_binding(tmp_path):
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    config = _echo_servers_config()
    config["demo"]["tool_effects"] = {"echo": "read_only", "add": "read_only"}
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=6000,
            max_entries=100,
            max_matches=30,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            mcp_servers=config,
        )
    )
    try:
        client = registry._mcp_clients[0]
        process = client._proc
        assert process is not None
        process.kill()
        process.wait(timeout=3)
        assert "mcp__demo__echo" not in registry.runtime_snapshot().available_tool_names

        registry.prepare_for_run()

        snapshot = registry.runtime_snapshot(run_id="mcp-reconnect-run")
        assert "mcp__demo__echo" in snapshot.available_tool_names
        call = canonical_test_call(
            snapshot,
            "mcp__demo__echo",
            {"text": "reconnected"},
        )
        result = registry.execute_tool(
            call,
            write_boundary=None,
            runtime_snapshot=snapshot,
        ).result
        assert result.ok is True
        assert result.output_trust == "external_data"
        assert '"result": "reconnected"' in result.output
    finally:
        registry.close_mcp_clients()


def _single_tool_server(tool_name: str) -> str:
    return textwrap.dedent(
        f"""
        import json, sys

        TOOL = {{
            "name": {tool_name!r},
            "description": "dynamic test tool",
            "inputSchema": {{"type": "object", "properties": {{}}}},
        }}

        def send(message):
            sys.stdout.write(json.dumps(message) + "\\n")
            sys.stdout.flush()

        for line in sys.stdin:
            request = json.loads(line)
            method = request.get("method")
            request_id = request.get("id")
            if method == "initialize":
                send({{"jsonrpc": "2.0", "id": request_id, "result": {{
                    "protocolVersion": "2024-11-05",
                    "capabilities": {{"tools": {{}}}},
                    "serverInfo": {{"name": "dynamic", "version": "1"}},
                }}}})
            elif method == "tools/list":
                send({{"jsonrpc": "2.0", "id": request_id, "result": {{"tools": [TOOL]}}}})
            elif method == "tools/call":
                send({{"jsonrpc": "2.0", "id": request_id, "result": {{
                    "content": [{{"type": "text", "text": TOOL["name"]}}],
                    "isError": False,
                }}}})
        """
    )


def test_registry_retains_failed_startup_and_recovers_on_later_run(tmp_path):
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    server = tmp_path / "late_server.py"
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=6000,
            max_entries=100,
            max_matches=30,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            mcp_servers={
                "late": {
                    "command": sys.executable,
                    "args": [str(server)],
                    "connect_timeout": 2,
                    "timeout": 2,
                    "tool_effects": {"ready": "read_only"},
                }
            },
        )
    )
    try:
        assert len(registry._mcp_clients) == 1
        assert not registry._mcp_clients[0].is_running()
        assert "mcp__late__ready" not in registry.tools

        server.write_text(_single_tool_server("ready"), encoding="utf-8")
        registry._mcp_retry_state.clear()
        registry.prepare_for_run()

        snapshot = registry.runtime_snapshot()
        assert "mcp__late__ready" in snapshot.available_tool_names
    finally:
        registry.close_mcp_clients()


def test_registry_reconnect_replaces_stale_mcp_catalog_exactly(tmp_path):
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    server = tmp_path / "changing_server.py"
    server.write_text(_single_tool_server("before"), encoding="utf-8")
    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=6000,
            max_entries=100,
            max_matches=30,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            mcp_servers={
                "changing": {
                    "command": sys.executable,
                    "args": [str(server)],
                    "connect_timeout": 2,
                    "timeout": 2,
                    "tool_effects": {
                        "before": "read_only",
                        "after": "read_only",
                    },
                }
            },
        )
    )
    try:
        assert "mcp__changing__before" in registry.runtime_snapshot().available_tool_names
        client = registry._mcp_clients[0]
        process = client._proc
        assert process is not None
        process.kill()
        process.wait(timeout=3)
        server.write_text(_single_tool_server("after"), encoding="utf-8")

        registry.prepare_for_run()

        names = registry.runtime_snapshot().available_tool_names
        assert "read_file" in names
        assert "mcp__changing__before" not in names
        assert "mcp__changing__after" in names
    finally:
        registry.close_mcp_clients()


def test_registry_failed_reconnect_uses_backoff_instead_of_retrying_every_lookup():
    from agent_py_agent.agent.tooling.registry import ToolRegistry

    class FailingClient:
        config = SimpleNamespace(name="failing")

        def __init__(self):
            self.reconnect_calls = 0
            self.stop_calls = 0

        def is_running(self):
            return False

        def reconnect(self):
            self.reconnect_calls += 1
            raise MCPError("still offline", code="MCP_CONNECTION_CLOSED")

        def stop(self):
            self.stop_calls += 1

    client = FailingClient()
    registry = object.__new__(ToolRegistry)
    registry.tools = {}
    registry._mcp_clients = [client]
    registry._mcp_prepare_lock = threading.Lock()
    registry._mcp_retry_state = {}

    registry.prepare_for_run()
    registry.prepare_for_run()

    assert client.reconnect_calls == 1
    assert client.stop_calls == 1
    attempts, retry_at = registry._mcp_retry_state[id(client)]
    assert attempts == 1
    assert retry_at > 0
