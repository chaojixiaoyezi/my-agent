"""W3 真实包/安装表/stdio 往返与确定性交错；W2 启用 UI 不在此分片。"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.capability.package_build import build_capability_pack
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import (
    PluginActivation,
    activation_catalog_digest,
    installation_tool_declarations,
    pinned_catalog_json_from_tools,
)
from agent_py_agent.agent.plugin_environment import EnvironmentBuildLimits
from agent_py_agent.agent.plugin_environment_plan import PluginEnvironmentPlan
from agent_py_agent.agent.plugin_files_environment import prepare_files_environment
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_runtime import PluginMCPClient
from agent_py_agent.agent.plugin_runtime_facts import resolve_plugin_runtime
from agent_py_agent.agent.tooling.mcp_client import MCPToolInfo
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home

_SERVER = '''import json, os, sys
from pathlib import Path
tools = __TOOLS__
data = Path(os.environ['MY_AGENT_PLUGIN_DATA_DIR'])
with (data / 'launches').open('a') as log:
    log.write(str(os.getpid()) + '\\n')
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    if method == 'notifications/initialized':
        continue
    if method == 'initialize':
        result = {'protocolVersion': request['params']['protocolVersion'],
                  'capabilities': {'tools': {}, 'experimental': {
                    'my-agent/workspace-read-context': {'versions': ['1']},
                    'my-agent/workspace-write-context': {'versions': ['1']}}},
                  'serverInfo': {'name': 'fixture', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': tools}
    elif method == 'tools/call':
        with (data / 'calls').open('a') as log:
            log.write(json.dumps(request['params']) + '\\n')
        result = {'content': [{'type': 'text', 'text': 'fixture-result'}],
                  'structuredContent': {'pid': os.getpid(), 'meta': request['params'].get('_meta')}}
    else:
        raise RuntimeError(method)
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
'''


# LLM: 测试安装与激活仍经真实 Store；仅 W1 旧 API 适配摘要，W2 原生校验不能被补丁覆盖。
# 函数用途: 准备兼容 W1 和 W2 激活接口的测试能力包。
def staged_pack(tmp_path, *, name="lazy-pack", tools=1, pinned=False, actual=None, owner=None, prepare=True):
    """安装与 preparing/active 使用真实 Store；只装配准备操作接口，不伪造安装表 JSON。"""
    owner = owner or resolve_owner_home(tmp_path / "home")
    store = PluginInstallStore(owner)
    declarations = [{"name": f"check_{index}", "description": f"检查 {index}",
                     "input_schema": {"type": "object", "properties": {}}, "requested_effect": "dangerous"}
                    for index in range(tools)]
    infos = [MCPToolInfo(row["name"], row["description"], row["input_schema"]) for row in declarations]
    wire = [{"name": row["name"], "description": row["description"], "inputSchema": row["input_schema"]}
            for row in declarations]
    contents = {"CAPABILITY.md": b"# Test pack\n", "mcp/server.py": _SERVER.replace(
        "__TOOLS__", repr(wire if actual is None else actual)).encode()}
    declaration = {"plugin_id": name, "version": "1.0.0", "summary": "按需检查",
                   "capability": {"description": "测试检查", "keywords": ["检查"], "entry_document": "CAPABILITY.md"},
                   "settings_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                   "files": [{"path": path} for path in contents],
                   "mcp_servers": [{"name": "checks", "root": "mcp", "entry": {
                       "kind": "interpreter", "command": "server.py", "args": [], "interpreter": "python3"},
                       "platforms": ["any"], "permissions": {"network": False},
                       "tool_catalog": "pinned_on_enable" if pinned else "declared", "tools": [] if pinned else declarations}]}
    package = inspect_plugin_package(build_capability_pack(declaration, contents).payload)
    entry = store.install(PluginInstallRequest(package, f"install-{name}", 0)).installation
    facts = resolve_plugin_runtime(entry.manifest)
    plan = PluginEnvironmentPlan(f"enable-{name}", name, entry.package_sha256, entry.revision,
                                 entry.settings_revision, facts.fingerprint)
    activation = PluginActivation(plan, "preparing")
    entry = store.change_activation(PluginActivationRequest(plan.operation_id, entry.revision, activation)).installation
    if prepare:
        operation = SimpleNamespace(owner=owner, plan=plan, authorize=lambda: None)
        prepare_files_environment(owner, package, operation, limits=EnvironmentBuildLimits())
    if pinned:
        activation = replace(activation, phase="active", catalog_sha256=activation_catalog_digest(
            entry.manifest, replace(activation, phase="active", catalog_sha256="a" * 64,
                                    pinned_catalog_json=pinned_catalog_json_from_tools(infos))),
                             pinned_catalog_json=pinned_catalog_json_from_tools(infos))
    else:
        activation = replace(activation, phase="active", catalog_sha256=activation_catalog_digest(entry.manifest, activation))
    if pinned:
        from agent_py_agent.agent import plugin_activation
        # 仅 W1 旧 API 需要摘要适配；W2 已原生支持固定目录的发布转换。
        if hasattr(plugin_activation, "plugin_catalog_digest"):
            with patch.object(plugin_activation, "plugin_catalog_digest", side_effect=lambda manifest: activation.catalog_sha256):
                entry = store.change_activation(PluginActivationRequest(plan.operation_id, entry.revision, activation)).installation
        else:
            entry = store.change_activation(PluginActivationRequest(plan.operation_id, entry.revision, activation)).installation
    else:
        entry = store.change_activation(PluginActivationRequest(plan.operation_id, entry.revision, activation)).installation
    return store, entry, owner


def pack_registry(tmp_path, owner, **changes):
    values = dict(workspace_root=tmp_path, max_chars=10000, max_entries=100, max_matches=100,
                  web_max_chars=10000, http_timeout=3, catalog_limit=100, retrieval_limit=10,
                  vector_search_enabled=False, plugin_owner=owner, capability_pack_mcp_enabled=True)
    return ToolRegistry(ToolRegistryParams(**(values | changes)))


def launches(owner, entry):
    path = owner.plugins_dir / "data" / entry.manifest.plugin_id / "launches"
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.sandbox_capability("background_launcher_identity", "nested_sandbox_exec")
def test_real_registry_first_call_reuses_idle_stop_restarts_without_context(tmp_path, pinned):
    _, entry, owner = staged_pack(tmp_path, pinned=pinned)
    registry = pack_registry(tmp_path, owner)
    try:
        registry.prepare_for_run()
        client = registry._mcp_clients[0]
        facade = client.pack_tools[0]
        assert client.lazy and client.config.connect_timeout == 10.0
        assert type(client.config.connect_timeout) is float
        assert facade.transport is None and not launches(owner, entry)
        assert facade.availability().available and facade.precheck_availability().available
        assert facade.model_spec.hints.provider_id == "package:lazy-pack"
        assert facade.model_spec.hints.default_deferred
        assert facade.runtime_policy.effect_resolver.default_effect == "dangerous"
        first = facade.execute({})
        assert first.ok, first
        first_pid = json.loads(first.output)["structuredContent"]["pid"]
        assert json.loads(first.output)["structuredContent"]["meta"] is None
        assert facade.execute({}).ok and len(launches(owner, entry)) == 1
        assert facade.transport is None and client._in_flight == 0
        receipt = client.idle_stop_if_due(client.last_used + 601, 600)
        assert receipt.confirmed and not client.is_closed() and not client.validated_tools
        assert facade.execute({}).ok
        assert len(launches(owner, entry)) == 2 and int(launches(owner, entry)[-1]) != first_pid
        assert client.stop().confirmed
        assert not facade.execute({}).ok and len(launches(owner, entry)) == 2
    finally:
        registry.close_mcp_clients()


@pytest.mark.sandbox_capability("background_launcher_identity", "nested_sandbox_exec")
def test_concurrent_first_calls_and_prepare_do_not_double_start(tmp_path, monkeypatch):
    _, entry, owner = staged_pack(tmp_path)
    registry = pack_registry(tmp_path, owner)
    entered, release = threading.Event(), threading.Event()
    try:
        registry.prepare_for_run()
        client = registry._mcp_clients[0]
        original = client.start
        starts = []
        def gated_start():
            starts.append(1)
            entered.set()
            assert release.wait(10)
            return original()
        monkeypatch.setattr(client, "start", gated_start)
        with ThreadPoolExecutor(max_workers=3) as pool:
            one = pool.submit(client.pack_tools[0].execute, {})
            assert entered.wait(5)
            two = pool.submit(client.pack_tools[0].execute, {})
            prepare = pool.submit(registry.prepare_for_run)
            release.set()
            assert one.result(timeout=15).ok and two.result(timeout=15).ok
            prepare.result(timeout=15)
        assert starts == [1] and len(launches(owner, entry)) == 1 and client._in_flight == 0
    finally:
        release.set()
        registry.close_mcp_clients()


@pytest.mark.sandbox_capability("background_launcher_identity", "nested_sandbox_exec")
def test_direct_ensure_starts_and_publishes_same_transport(tmp_path):
    _, entry, owner = staged_pack(tmp_path)
    client = PluginMCPClient(owner, entry)
    try:
        transport = client.ensure_started()
        assert client.is_running() and client.validated_tools
        assert all(tool.transport is transport for tool in client.validated_tools)
    finally:
        client.stop()


class ComponentTransport:
    """只供状态机交错的内存连接；不作为真实 stdio 或 OS 清理证据。"""
    def __init__(self):
        self.inbox = SimpleNamespace(closed=threading.Event(), tools_changed=False)
        self.admission_lock = threading.RLock()
        self.capabilities = {}
        self.ready = True
        self.cleanup_confirmed = True
        self.stops = 0

    def is_running(self):
        return not self.inbox.closed.is_set()

    def revoke(self):
        self.inbox.closed.set()

    def terminate(self, **kwargs):
        self.stops += 1
        self.revoke()
        return SimpleNamespace(confirmed=self.cleanup_confirmed)


def component_client(tmp_path, monkeypatch, *, pinned=False):
    """真实安装/代理/发布门，内存传输隔离状态机；不测试平台沙箱或托管启动身份。"""
    from agent_py_agent.agent import plugin_runtime, plugin_sandbox
    monkeypatch.setattr(plugin_sandbox, "plugin_sandbox_problem", lambda *args, **kwargs: "")
    monkeypatch.setattr(plugin_runtime, "plugin_sandbox_problem", lambda *args, **kwargs: "")
    monkeypatch.setattr(plugin_runtime, "sandboxed_plugin_argv", lambda argv, spec: list(argv))
    store, entry, owner = staged_pack(tmp_path, pinned=pinned)
    client = PluginMCPClient(owner, entry)
    starts, calls = [], []
    def start():
        transport = ComponentTransport()
        client._transport = transport
        starts.append(transport)
        return transport
    def call(name, arguments, **kwargs):
        calls.append((name, arguments, kwargs))
        return {"content": "component-result"}
    monkeypatch.setattr(client, "start", start)
    monkeypatch.setattr(client, "list_tools", lambda **kwargs: [
        MCPToolInfo(tool.name, tool.description, tool.input_schema) for tool in installation_tool_declarations(entry)])
    monkeypatch.setattr(client, "call_tool", call)
    return client, store, entry, owner, starts, calls


def test_component_concurrent_calls_and_idle_reservation(tmp_path, monkeypatch):
    client, _, _, _, starts, calls = component_client(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = client.start
    def start():
        entered.set()
        assert release.wait(5)
        return original()
    monkeypatch.setattr(client, "start", start)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(client.pack_tools[0].execute, {})
            assert entered.wait(3)
            two = pool.submit(client.pack_tools[0].execute, {})
            release.set()
            assert one.result(timeout=5).ok
            assert two.result(timeout=5).ok
        assert len(starts) == 1 and len(calls) == 2
        assert client._in_flight == 0 and client.pack_tools[0].transport is None
        assert client.idle_stop_if_due(client.last_used + 601, 600).confirmed
        assert not client.is_closed() and not client.validated_tools
        assert client.pack_tools[0].execute({}).ok and len(starts) == 2
    finally:
        release.set()
        client.stop()


def test_component_idle_does_not_stop_pending_call(tmp_path, monkeypatch):
    client, _, _, _, starts, calls = component_client(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    call = client.call_tool
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return call(*args, **kwargs)
    monkeypatch.setattr(client, "call_tool", blocked)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(client.pack_tools[0].execute, {})
            assert entered.wait(3)
            assert client._in_flight == 1
            assert client.idle_stop_if_due(client.last_used + 601, 600) is None
            assert starts[0].stops == 0
            release.set()
            assert result.result(timeout=5).ok
        assert len(calls) == 1 and client._in_flight == 0
        assert client.idle_stop_if_due(client.last_used + 601, 600).confirmed
    finally:
        release.set()
        client.stop()
