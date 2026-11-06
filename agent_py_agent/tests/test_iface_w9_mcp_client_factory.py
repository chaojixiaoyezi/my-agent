# LLM: 混合真实 stdio 与独立协议替身验证宿主工厂接缝；不把替身结果外推为共享池隔离。
# 模块用途: 回归工厂默认值、生命周期、异常隔离和原执行权限链。
"""工厂接缝的注册、生命周期与原宿主权限回归；不代表共享池工厂隔离已闭合。"""
from __future__ import annotations

import inspect
import json
import sys
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling import mcp_registration
from agent_py_agent.agent.tooling import registry as registry_module
from agent_py_agent.agent.tooling.mcp_client import (
    MCPError,
    MCPServerConfig,
    MCPStdioClient,
    MCPToolInfo,
)
from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
from agent_py_agent.tests.test_mcp_client import _ECHO_SERVER


# LLM: 只实现注册与生命周期所需协议，不继承或改写生产客户端，不启动进程。
# 类用途: 提供可注入故障和关闭回执的独立客户端替身。
class ProtocolClient:
    """独立的结构化客户端替身；不继承 stdio 实现，也不启动进程。"""

    # LLM: 配置与工具声明独立保存；受控回执不代表真实进程关闭证明。
    # 函数用途: 建立协议替身和测试可观测状态。
    def __init__(self, config):
        self.config = config
        self.transport = SimpleNamespace(connection_id="factory-connection-1")
        self.publication = None
        self.tools_changed = False
        self.running = False
        self.closed = False
        self.start_failures = 0
        self.start_calls = 0
        self.reconnect_calls = 0
        self.calls = []
        self.disconnected = []
        self.stop_calls = 0
        self.discovery_error = None
        self.publication_error = None
        self.receipt = ProcessTerminationReceipt("protocol-close", True, 0, 1)
        self.tools = [MCPToolInfo("echo", "echo", {
            "type": "object", "properties": {"text": {"type": "string"}},
            "required": ["text"], "additionalProperties": False,
        })]

    # LLM: 只改变替身状态，可按次数模拟暂时启动失败。
    # 函数用途: 返回固定连接或抛出受控启动异常。
    def start(self):
        self.start_calls += 1
        if self.start_failures:
            self.start_failures -= 1
            raise MCPError("controlled startup failure", code="MCP_SERVER_START_FAILED")
        self.running = True
        return self.transport

    # LLM: 重连仍使用同一客户端，不通过工厂重新构造。
    # 函数用途: 记录重连次数并复用启动行为。
    def reconnect(self):
        self.reconnect_calls += 1
        return self.start()

    # LLM: 纯内存就绪查询，不重连或启动服务。
    # 函数用途: 返回替身是否仍在运行。
    def is_running(self):
        return self.running and not self.closed

    # LLM: 只读关闭状态，与暂时启动失败明确区分。
    # 函数用途: 返回替身是否已经永久关闭。
    def is_closed(self):
        return self.closed

    # LLM: 只返回当前连接，不替不可用代理创建新连接。
    # 函数用途: 暴露就绪连接供代理核对身份。
    def connection(self):
        if not self.is_running():
            raise MCPError("closed", code="MCP_CONNECTION_CLOSED")
        return self.transport

    # LLM: 发现必须使用指定原连接，可模拟发现阶段失败。
    # 函数用途: 返回受控工具声明列表。
    def list_tools(self, *, transport):
        assert transport is self.transport
        if self.discovery_error is not None:
            raise self.discovery_error
        return self.tools

    # LLM: 发布之前复核连接并先触发故障，失败不得调用目录提交回调。
    # 函数用途: 模拟原子发布边界。
    def publish_tools(self, transport, publisher):
        assert transport is self.transport
        if self.publication_error is not None:
            raise self.publication_error
        return publisher()

    # LLM: 发送前执行原宿主 authority_check；返回的测试密钥必须被代理脱敏。
    # 函数用途: 记录实际到达客户端的工具参数并返回受控结果。
    def call_tool(self, tool_name, arguments, *, transport, authority_check=None):
        assert transport is self.transport
        if authority_check is not None:
            authority_check()
        self.calls.append((tool_name, arguments))
        return {"content": arguments["text"], "isError": False,
                "structuredContent": {"api_key": "controlled-test-secret"}}

    # LLM: 仅断开指定连接，原关闭回执不因 confirmed 为假而被改写。
    # 函数用途: 记录清理目标并保留客户端供后续恢复。
    def disconnect(self, *, transport):
        assert transport is self.transport
        self.disconnected.append(transport)
        self.running = False
        return self.receipt

    # LLM: 永久关闭与临时断开分开记录，不产生真实进程副作用。
    # 函数用途: 记录生命周期管理是否回收了原客户端。
    def stop(self):
        self.stop_calls += 1
        self.closed = True
        self.running = False
        return self.receipt


# LLM: 工厂本身是假值可调用对象，用于检测错误的 `factory or default` 实现。
# 类用途: 记录构造实例并在启动前注入测试故障。
class RecordingFactory:
    # LLM: 可选配置回调仅接收新建替身，不修改宿主装配。
    # 函数用途: 保存工厂调用记录和故障配置回调。
    def __init__(self, configure=lambda client: None):
        self.clients = []
        self.configure = configure

    # LLM: 假值并不代表缺少工厂；生产逻辑只能对 None 使用默认值。
    # 函数用途: 让工厂在布尔上下文中为假。
    def __bool__(self):
        return False

    # LLM: 每次构造独立协议客户端并记录身份，供生命周期断言复核。
    # 函数用途: 按宿主传入配置建立一个客户端。
    def __call__(self, config):
        client = ProtocolClient(config)
        self.configure(client)
        self.clients.append(client)
        return client


# LLM: 只使用本地受控 echo 服务；配置覆盖用于检查 raw 映射不能注入宿主字段。
# 函数用途: 生成测试用 MCP 服务配置。
def _servers(**overrides):
    return {"demo": {"command": sys.executable, "args": ["-c", _ECHO_SERVER],
                     "connect_timeout": 5, "timeout": 5, **overrides}}


# LLM: 先核对公开可选接缝再调用真实注册入口，不 monkeypatch 生产注册逻辑。
# 函数用途: 用指定工厂注册一组测试服务。
def _injected(registry, factory, servers=None, **kwargs):
    # 缺功能时先以接口契约断言失败，不让未知 keyword 的 TypeError 遮蔽意图。
    assert "mcp_client_factory" in inspect.signature(mcp_registration.register_mcp_servers).parameters
    return mcp_registration.register_mcp_servers(
        registry, _servers() if servers is None else servers,
        mcp_client_factory=factory, **kwargs,
    )


# LLM: 延后连接并关闭持久操作账要求，仅在 pytest 临时工作区构造真实 registry。
# 函数用途: 提供可验证原重连和关闭路径的注册表。
def _registry(tmp_path):
    return ToolRegistry(ToolRegistryParams(
        workspace_root=tmp_path, max_chars=1000, max_entries=20, max_matches=10,
        web_max_chars=1000, http_timeout=5, catalog_limit=10, retrieval_limit=3,
        vector_search_enabled=False, defer_mcp_connect=True,
        operation_store_required=False, operation_owner_id="factory-owner",
    ))


# LLM: 工厂接缝属于宿主 API，必须保留可省略的 keyword-only 默认参数及类型别名。
# 函数用途: 核对新增接口的兼容签名。
def test_factory_contract_is_optional_keyword_only_and_typed():
    parameter = inspect.signature(mcp_registration.register_mcp_servers).parameters.get("mcp_client_factory")
    assert parameter is not None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None
    assert mcp_registration.MCPClientFactory == Callable[[MCPServerConfig], MCPStdioClient]


# LLM: 真实 stdio 进程需完成注册、调用及 confirmed 关闭；不跳过或放宽关闭断言。
# 函数用途: 核对省略工厂和显式 None 的默认端到端行为。
@pytest.mark.parametrize("explicit_none", [False, True])
def test_default_factory_preserves_real_stdio_roundtrip_and_close_receipt(explicit_none):
    registry = SimpleNamespace(tools={})
    kwargs = {"mcp_client_factory": None} if explicit_none else {}
    if explicit_none:
        assert "mcp_client_factory" in inspect.signature(mcp_registration.register_mcp_servers).parameters
    clients = mcp_registration.register_mcp_servers(registry, _servers(), **kwargs)
    client = clients[0]
    process = client.connection().binding.process
    try:
        assert type(client) is MCPStdioClient
        assert set(registry.tools) == {"mcp__demo__echo", "mcp__demo__add"}
        result = registry.tools["mcp__demo__echo"].execute({"text": "default-roundtrip"})
        assert result.ok and json.loads(result.output)["result"] == "default-roundtrip"
    finally:
        receipt = client.stop()
        assert receipt.confirmed and process.poll() is not None
    assert client.is_closed()


# LLM: raw 配置不能覆盖宿主环境来源或选择工厂，代理必须保留 canonical schema 和脱敏。
# 函数用途: 验证假值工厂与非继承协议客户端真正接入原代理链。
def test_falsy_protocol_factory_receives_host_config_and_publishes_real_proxy():
    sentinel = object()
    registry = SimpleNamespace(tools={"core": sentinel})
    factory = RecordingFactory()
    clients = _injected(registry, factory, _servers(
        tool_effects={"echo": "read_only"}, host_credential_env_names=["FORGED"],
        mcp_client_factory="untrusted-mapping-value",
    ), host_credential_env_names=frozenset({"HOST_ONLY"}))
    assert clients == factory.clients and len(clients) == 1
    client = clients[0]
    assert not isinstance(client, MCPStdioClient)
    assert client.config.host_credential_env_names == frozenset({"HOST_ONLY"})
    assert client.config.command == sys.executable
    assert client.config.args == ["-c", _ECHO_SERVER]
    assert registry.tools["core"] is sentinel
    proxy = registry.tools["mcp__demo__echo"]
    assert proxy.client is client and proxy.transport is client.transport
    assert proxy.runtime_policy.effect_resolver.default_effect == "read_only"
    assert proxy.model_spec.input_schema["properties"] == {"text": {"type": "string"}}
    result = proxy.execute({"tool": "mcp__demo__echo", "text": "custom", "__run_scope": {"run_id": "private"}})
    assert result.ok and client.calls == [("echo", {"text": "custom"})]
    payload = json.loads(result.output)
    assert payload["result"] == "custom"
    assert payload["structuredContent"]["api_key"] == "<redacted>"
    assert client.publication.status == "published" and client.publication.tool_count == 1


# LLM: 空配置和非法配置保持惰性，不调用工厂或发布工具。
# 函数用途: 验证无服务时的零构造路径。
@pytest.mark.parametrize("servers", [None, {}, [], {"bad": {}}, {"bad": "not-a-mapping"}])
def test_empty_or_invalid_config_never_calls_injected_factory(servers):
    factory = RecordingFactory()
    registry = SimpleNamespace(tools={})
    # None 的服务器值需要直接传给入口，不采用 helper 的默认配置。
    assert "mcp_client_factory" in inspect.signature(mcp_registration.register_mcp_servers).parameters
    assert mcp_registration.register_mcp_servers(registry, servers, mcp_client_factory=factory) == []
    assert factory.clients == [] and registry.tools == {}


# LLM: start 失败的客户端仍须返回，健康服务继续发布，不能提前永久关闭失败客户端。
# 函数用途: 检查启动失败保留与多服务隔离。
def test_failed_startup_retains_injected_client_without_affecting_other_server():
    # LLM: 故障只作用于指定替身，不影响另一服务。
    # 函数用途: 让 broken 客户端第一次启动失败。
    def configure(client):
        if client.config.name == "broken":
            client.start_failures = 1

    factory = RecordingFactory(configure)
    registry = SimpleNamespace(tools={})
    configs = {name: {"command": sys.executable} for name in ("broken", "healthy")}
    clients = _injected(registry, factory, configs)
    assert clients == factory.clients and len(clients) == 2
    failed, healthy = clients
    assert failed.publication.status == "unavailable"
    assert failed.publication.code == "MCP_SERVER_START_FAILED"
    assert not failed.is_running() and not failed.is_closed()
    assert failed.stop_calls == 0
    assert healthy.publication.status == "published" and healthy.is_running()
    assert set(registry.tools) == {"mcp__healthy__echo"}


# LLM: 构造失败只隔离显式注入工厂的当前服务，不丢此前客户端或阻断后续服务。
# 函数用途: 验证自定义工厂抛异常时已有和后续客户端仍交回生命周期管理。
@pytest.mark.parametrize("error_type", [MCPError, RuntimeError])
def test_custom_factory_constructor_failure_keeps_other_clients(error_type):
    constructed = []

    # LLM: 独立协议替身不启动进程；异常发生于中间服务的构造而非 start。
    # 函数用途: 在已创建一个客户端之后抛出受控构造异常。
    def factory(config):
        if config.name == "broken":
            raise error_type("controlled factory construction failure")
        client = ProtocolClient(config)
        constructed.append(client)
        return client

    registry = SimpleNamespace(tools={})
    servers = {name: {"command": sys.executable} for name in ("first", "broken", "last")}
    clients = _injected(registry, factory, servers)
    assert clients == constructed and len(clients) == 2
    assert [client.config.name for client in clients] == ["first", "last"]
    assert all(client.is_running() and client.stop_calls == 0 for client in clients)
    assert set(registry.tools) == {"mcp__first__echo", "mcp__last__echo"}


# LLM: 未注入工厂时保留原构造异常传播，不能借新接缝改变旧调用者的异常合同。
# 函数用途: 核对省略参数和显式 None 都不吞掉默认构造异常。
@pytest.mark.parametrize("explicit_none", [False, True])
def test_default_constructor_exception_keeps_existing_propagation(monkeypatch, explicit_none):
    error = RuntimeError("controlled default constructor failure")

    # LLM: 替身只用于定位构造异常，不代替真实 stdio 关闭测试。
    # 函数用途: 模拟默认客户端构造器原有的异常出口。
    def fail(config):
        raise error

    monkeypatch.setattr(mcp_registration, "MCPStdioClient", fail)
    kwargs = {"mcp_client_factory": None} if explicit_none else {}
    with pytest.raises(RuntimeError) as caught:
        mcp_registration.register_mcp_servers(SimpleNamespace(tools={}), _servers(), **kwargs)
    assert caught.value is error


# LLM: 使用真实 registry.prepare_for_run 与可控时钟验证退避，不能重新调用工厂取代原实例。
# 函数用途: 检查失败客户端恢复、重试节流和最终关闭。
def test_injected_client_reconnects_on_original_backoff_without_reconstruction(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    factory = RecordingFactory(lambda client: setattr(client, "start_failures", 2))
    try:
        clients = _injected(registry, factory)
        registry._mcp_clients.extend(clients)
        registry._mcp_connected_state[0] = True
        clock = [100.0]
        monkeypatch.setattr(registry_module.time, "monotonic", lambda: clock[0])
        client = clients[0]
        registry.prepare_for_run()
        assert client.reconnect_calls == 1
        assert registry._mcp_retry_state[id(client)] == (1, 101.0)
        registry.prepare_for_run()
        assert client.reconnect_calls == 1
        clock[0] = 101.0
        registry.prepare_for_run()
        assert client.reconnect_calls == 2 and client.start_calls == 3
        assert factory.clients == [client] and registry._mcp_clients == [client]
        assert registry._mcp_retry_state == {}
        assert {name for name in registry.tools if name.startswith("mcp__")} == {"mcp__demo__echo"}
    finally:
        registry.close_mcp_clients()
    assert client.stop_calls == 1 and client.is_closed()
    assert registry._mcp_clients == []


# LLM: 发现或发布失败必须保留原目录和原关闭事实，不能把未知清理改成成功。
# 函数用途: 验证各失败分支的目录原子性与指定连接清理。
@pytest.mark.parametrize("failure", ["discovery", "publication", "unexpected"])
@pytest.mark.parametrize("confirmed", [False, True])
def test_failed_discovery_or_publication_keeps_atomic_catalog_and_close_fact(failure, confirmed):
    # LLM: 在原发布边界注入失败与不同关闭回执，不替换生产发布函数。
    # 函数用途: 设置发现、发布或意外异常。
    def configure(client):
        client.receipt = ProcessTerminationReceipt("controlled-cleanup", confirmed, None, 1)
        if failure == "discovery":
            client.discovery_error = MCPError("discovery failed")
        elif failure == "unexpected":
            client.discovery_error = RuntimeError("controlled unexpected failure")
        else:
            client.publication_error = MCPError("connection revoked", code="MCP_CONNECTION_CLOSED")

    factory = RecordingFactory(configure)
    original = {"core": object()}
    registry = SimpleNamespace(tools=original)
    clients = _injected(registry, factory)
    client = clients[0]
    assert registry.tools is original and list(registry.tools) == ["core"]
    assert clients == [client] and factory.clients == [client]
    assert client.disconnected == [client.transport]
    assert client.receipt.confirmed is confirmed
    assert client.stop_calls == 0 and not client.closed
    assert client.reconnect_calls == 0


# LLM: 旧代理绑定原连接；新连接候选失败不得替换目录，成功后才提交完整目录。
# 函数用途: 验证刷新原子性和旧代理不跟随新连接。
def test_factory_refresh_replaces_catalog_only_after_publication_and_freezes_transport():
    factory = RecordingFactory()
    registry = SimpleNamespace(tools={"core": object()})
    client = _injected(registry, factory)[0]
    old_catalog = registry.tools
    old_proxy = old_catalog["mcp__demo__echo"]
    old_transport = client.transport
    client.transport = SimpleNamespace(connection_id="factory-connection-2")
    client.tools = [MCPToolInfo("replacement", "new generation", {"type": "object", "properties": {}})]
    client.publication_error = MCPError("revoked before publish", code="MCP_CONNECTION_CLOSED")
    with pytest.raises(MCPError, match="revoked"):
        mcp_registration.refresh_registered_mcp_client(registry, client, transport=client.transport)
    assert registry.tools is old_catalog
    assert old_proxy.transport is old_transport
    assert not old_proxy.availability().available
    client.publication_error = None
    assert mcp_registration.refresh_registered_mcp_client(registry, client, transport=client.transport) == 1
    assert registry.tools is not old_catalog
    assert set(registry.tools) == {"core", "mcp__demo__replacement"}
    assert registry.tools["mcp__demo__replacement"].transport is client.transport
    assert not old_proxy.availability().available


# LLM: 拒绝必须发生在 canonical executor 的真实 handler 之前，工厂不能增加执行权限。
# 函数用途: 核对审批、运行授权和 schema 拒绝仍生效。
@pytest.mark.parametrize(("effect", "arguments", "allowed", "code"), [
    ("dangerous", {"text": "blocked"}, None, "APPROVAL_REQUIRED"),
    ("read_only", {"text": "blocked"}, [], "TOOL_NOT_IN_RUNTIME_SNAPSHOT"),
    ("read_only", {"text": {}}, None, "TOOL_PARAMETER_TYPE_INVALID"),
    ("read_only", {"text": "blocked", "mcp_client_factory": "forged"}, None, "TOOL_INVALID_ARGUMENTS"),
])
def test_injected_factory_does_not_bypass_host_approval_scope_or_schema(tmp_path, effect, arguments, allowed, code):
    registry = SimpleNamespace(tools={})
    factory = RecordingFactory()
    client = _injected(registry, factory, _servers(default_effect=effect))[0]
    result = execute_canonical_test_call(
        tmp_path, tools=registry.tools, tool_name="mcp__demo__echo",
        arguments=arguments, allowed_tools=allowed,
    ).result
    assert not result.ok and result.error_code == code
    assert result.handler_executed is False
    assert client.calls == []


# LLM: 发送前复核同一操作权限，输出保持外部信任级别并脱敏受控密钥。
# 函数用途: 验证成功调用仍经过原宿主 authority callback。
def test_injected_factory_preserves_scoped_authority_callback(tmp_path):
    registry = SimpleNamespace(tools={})
    factory = RecordingFactory()
    client = _injected(registry, factory, _servers(default_effect="read_only"))[0]
    checked = []
    store = SimpleNamespace(require_authority=checked.append)
    result = execute_canonical_test_call(
        tmp_path, tools=registry.tools, tool_name="mcp__demo__echo", arguments={"text": "scoped"},
        operation_store=store,
    ).result
    assert result.ok and result.handler_executed
    assert client.calls == [("echo", {"text": "scoped"})]
    assert len(checked) >= 2
    assert checked[0] == checked[-1]
    assert result.output_trust == "external_data"
    assert "controlled-test-secret" not in result.output


# LLM: 模型只看到远端工具的业务字段，不能从工具 JSON 注入宿主工厂。
# 函数用途: 精确比较模型可见参数 Schema。
def test_model_schema_never_exposes_host_factory_configuration():
    factory = RecordingFactory()
    registry = SimpleNamespace(tools={})
    _injected(registry, factory)
    schema = registry.tools["mcp__demo__echo"].model_spec.input_schema
    assert schema == {
        "type": "object", "properties": {"text": {"type": "string"}},
        "required": ["text"], "additionalProperties": False,
    }
