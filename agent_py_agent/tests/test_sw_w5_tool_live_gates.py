"""SW-W5：七个实时属性、快照门和运行边界客户端同步的行为合同。"""
from __future__ import annotations

import json
from dataclasses import fields
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import core
from agent_py_agent.agent.agent_core import orchestration_tools
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import plugin_registration
from agent_py_agent.agent.tooling import registry as registry_module
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    ToolAvailability,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

PROPERTIES = (
    ("catalog_include_examples", "tool_catalog_include_examples"),
    ("vector_search_enabled", "tool_vector_search_enabled"),
    ("background_process_listen_scope_enforce", "background_process_listen_scope_enforce"),
    ("shell_sandbox_boundary_facts", "shell_sandbox_boundary_facts"),
    ("enable_file_syntax_diagnostics", "enable_file_syntax_diagnostics"),
    ("plugin_process_sandbox", "plugin_process_sandbox"),
    ("capability_pack_mcp_enabled", "capability_pack_mcp_enabled"),
)
GATES = (
    "enable_gateway_restart_tool", "enable_memory_search_tool", "enable_model_profile_tool",
    "enable_subagents", "tool_program_enabled", "feishu_chat_history_enabled",
)


def make_registry(tmp_path, **overrides):
    values = dict(workspace_root=tmp_path, max_chars=1000, max_entries=10, max_matches=10,
                  web_max_chars=1000, http_timeout=1, catalog_limit=100, retrieval_limit=10,
                  vector_search_enabled=False)
    values.update(overrides)
    return ToolRegistry(ToolRegistryParams(**values))


class DeferredProbe(BaseTool):
    model_spec = ToolModelSpec(
        name="sw_live_probe", description="实时开关合同探针", input_schema={"type": "object", "properties": {}},
        hints=ToolModelHints(category="general", default_deferred=True, deferred_summary="测试默认折叠"))
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("read_only"))


@pytest.mark.parametrize(("attribute", "key"), PROPERTIES)
def test_property_reads_live_value_and_is_read_only(tmp_path, attribute, key):
    config = SimpleNamespace(**{key: False})
    registry = make_registry(tmp_path, live_config=config)
    assert getattr(registry, attribute) is False
    setattr(config, key, True)
    assert getattr(registry, attribute) is True
    setattr(config, key, False)
    assert getattr(registry, attribute) is False
    with pytest.raises(AttributeError):
        setattr(registry, attribute, True)


@pytest.mark.parametrize(("attribute", "key"), PROPERTIES)
def test_property_falls_back_to_params_without_live_config(tmp_path, attribute, key):
    registry = make_registry(tmp_path, **{attribute: True})
    assert getattr(registry, attribute) is True


@pytest.mark.parametrize("gate", GATES)
def test_live_gate_next_snapshot_and_existing_snapshot_are_independent(tmp_path, gate):
    agent_config = SimpleNamespace(**{gate: False})
    capability = SimpleNamespace(**{gate: False})
    registry = make_registry(tmp_path, live_config=agent_config, capability_provider=lambda: capability)
    tool = DeferredProbe()
    registry.register(tool, live_gate=gate)
    first = registry.runtime_snapshot()
    assert first.runtime("sw_live_probe") is None
    assert ("sw_live_probe", "TOOL_UNAVAILABLE", "disabled by " + gate) in first.unavailable_tools
    assert registry.tools["sw_live_probe"] is tool
    source = capability if gate in {"tool_program_enabled", "feishu_chat_history_enabled"} else agent_config
    setattr(source, gate, True)
    second = registry.runtime_snapshot()
    assert second.runtime("sw_live_probe") is not None
    assert first.runtime("sw_live_probe") is None
    setattr(source, gate, False)
    assert registry.runtime_snapshot().runtime("sw_live_probe") is None
    assert second.runtime("sw_live_probe") is not None
    view = registry.with_access_policy(access_mode="restricted", path_access_mode="normal", owner_scope_root="")
    assert view.runtime_snapshot().runtime("sw_live_probe") is None
    setattr(source, gate, True)
    assert view.runtime_snapshot().runtime("sw_live_probe") is not None
    view.close_mcp_clients()
    registry.close_mcp_clients()


def test_gate_does_not_override_tool_own_availability(tmp_path):
    registry = make_registry(tmp_path, live_config=SimpleNamespace(enable_subagents=True))
    tool = DeferredProbe()
    tool.availability = lambda: ToolAvailability.unavailable("structural restriction")
    registry.register(tool, live_gate="enable_subagents")
    assert ("sw_live_probe", "TOOL_UNAVAILABLE", "structural restriction") in registry.runtime_snapshot().unavailable_tools


def test_capability_provider_failure_is_unavailable(tmp_path):
    def broken():
        raise OSError("unreadable")
    registry = make_registry(tmp_path, capability_provider=broken)
    registry.register(DeferredProbe(), live_gate="tool_program_enabled")
    assert registry.runtime_snapshot().runtime("sw_live_probe") is None


def test_default_deferral_is_fixed_on_and_discoverable(tmp_path):
    registry = make_registry(tmp_path)
    registry.register(DeferredProbe())
    snapshot = registry.runtime_snapshot()
    assert snapshot.default_deferral_enabled is True
    assert "sw_live_probe" not in {spec.name for spec in registry.model_visible_specs(runtime_snapshot=snapshot)}
    assert "sw_live_probe" in registry.render_catalog_section(runtime_snapshot=snapshot)
    assert "sw_live_probe" in {spec.name for spec in registry.search_deferred_specs("sw_live_probe")}
    names = {item.name for item in fields(ToolRegistryParams)}
    assert "catalog_default_deferral_enabled" not in names
    assert "catalog_show_truncated_notice" not in names
    assert not hasattr(registry, "catalog_show_truncated_notice")


def test_mcp_default_is_deferred_until_explicit_ensure(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(registry_module, "_connect_mcp_servers", lambda registry, servers: calls.append(servers) or [])
    registry = make_registry(tmp_path, mcp_servers={"sample": {"command": "not-started"}})
    assert calls == []
    assert registry.ensure_mcp_connected() is True
    assert calls == [{"sample": {"command": "not-started"}}]
    assert registry.ensure_mcp_connected() is False
    assert len(calls) == 1
    registry.close_mcp_clients()


def test_real_agent_registration_and_gateway_restart_live_flip(tmp_path, monkeypatch):
    capability = core.CapabilityConfig(tool_program_enabled=True, feishu_chat_history_enabled=False)
    monkeypatch.setattr(core, "capability_config_for_agent", lambda agent: capability)
    agent = core.SimpleAgent(AgentConfig(enable_plugins=False, enable_gateway_restart_tool=False,
                                         memory_path=str(tmp_path / "memory.jsonl")), tmp_path)
    registry = agent.tools
    assert registry._construction_params.live_config is agent.config
    assert "restart_gateway" in registry.tools
    assert registry.runtime_snapshot().runtime("restart_gateway") is None
    agent.config.enable_gateway_restart_tool = True
    assert registry.runtime_snapshot().runtime("restart_gateway") is not None
    assert registry._live_tool_gates["memory_search"] == "enable_memory_search_tool"
    assert registry._live_tool_gates["manage_models"] == "enable_model_profile_tool"
    assert registry._live_tool_gates["create_subagents"] == "enable_subagents"
    assert registry._live_tool_gates["tool_program"] == "tool_program_enabled"
    assert registry._live_tool_gates["channel_messages"] == "feishu_chat_history_enabled"
    registry.close_mcp_clients()


def test_media_schema_reads_capability_after_construction(monkeypatch):
    capability = SimpleNamespace(subagent_input_media_enabled=False)
    monkeypatch.setattr(orchestration_tools, "capability_config_for_agent", lambda agent: capability)
    tool = orchestration_tools.CreateSubagentsTool(SimpleNamespace())
    assert "input_media_refs" not in json.dumps(tool.model_spec.input_schema)
    capability.subagent_input_media_enabled = True
    assert "input_media_refs" in json.dumps(tool.model_spec.input_schema)
    capability.subagent_input_media_enabled = False
    assert "input_media_refs" not in json.dumps(tool.model_spec.input_schema)


class FakeClient:
    """只模拟结构化清理回执，不启动进程或网络连接。"""
    def __init__(self, name, events, confirmed=True):
        self.config = SimpleNamespace(name=name)
        self.events = events
        self.closed = False
        self.confirmed = confirmed
        self.tools_changed = False

    def is_running(self):
        return not self.closed

    def is_closed(self):
        return self.closed

    def stop(self):
        self.events.append(("stop", self.config.name))
        self.closed = self.confirmed
        return SimpleNamespace(confirmed=self.confirmed)


def test_computer_profiles_and_enable_tools_follow_next_sync(tmp_path, monkeypatch):
    config = SimpleNamespace(mcp_servers={"ordinary": {"command": "not-executed"}},
                             computer_use_enabled=False, computer_use_observation_enabled=False, enable_tools=True)
    monkeypatch.setattr(core, "is_local_admin_owner", lambda home: True)
    events = []
    seen = []
    def connect(registry, servers):
        seen.append(servers)
        return [FakeClient(name, events) for name in servers]
    monkeypatch.setattr(registry_module, "_connect_mcp_servers", connect)
    registry = make_registry(tmp_path, live_config=config, mcp_servers_provider=lambda: core._tool_registry_mcp_servers(
        SimpleNamespace(home_paths=None), config, "full-access"))
    assert seen == []
    registry.prepare_for_run()
    ordinary = registry._mcp_clients[0]
    assert [client.config.name for client in registry._mcp_clients] == ["ordinary"]
    config.computer_use_observation_enabled = True
    registry.prepare_for_run()
    observer = next(client for client in registry._mcp_clients if client.config.name == "computer_use")
    profile = registry._mcp_servers_state[0]["computer_use"]
    assert profile["env"]["MY_AGENT_COMPUTER_USE_OBSERVE_ONLY"] == "1"
    assert set(profile["tool_effects"]) == {"observe_window"}
    config.computer_use_enabled = True
    registry.prepare_for_run()
    assert observer.closed
    controller = next(client for client in registry._mcp_clients if client.config.name == "computer_use")
    profile = registry._mcp_servers_state[0]["computer_use"]
    assert "click_candidate" in profile["tool_effects"]
    assert registry._mcp_clients[0] is ordinary
    config.computer_use_observation_enabled = False
    registry.prepare_for_run()
    assert controller.closed
    assert "observe_window" not in registry._mcp_servers_state[0]["computer_use"]["tool_effects"]
    config.computer_use_enabled = False
    registry.prepare_for_run()
    assert [client.config.name for client in registry._mcp_clients] == ["ordinary"]
    config.enable_tools = False
    registry.prepare_for_run()
    assert ordinary.closed and registry._mcp_clients == []
    config.enable_tools = True
    registry.prepare_for_run()
    assert [client.config.name for client in registry._mcp_clients] == ["ordinary"]
    assert registry._mcp_clients[0] is not ordinary
    registry.close_mcp_clients()


def test_computer_profile_never_ignores_structural_owner_boundary(tmp_path, monkeypatch):
    config = SimpleNamespace(mcp_servers={}, computer_use_enabled=True, computer_use_observation_enabled=True)
    monkeypatch.setattr(core, "is_local_admin_owner", lambda home: False)
    assert core._tool_registry_mcp_servers(SimpleNamespace(home_paths=None), config, "full-access") == {}
    monkeypatch.setattr(core, "is_local_admin_owner", lambda home: True)
    assert core._tool_registry_mcp_servers(SimpleNamespace(home_paths=None), config, "workspace-write") == {}


def test_mcp_cleanup_unknown_blocks_replacement(tmp_path, monkeypatch):
    selected = {"sample": {"command": "first"}}
    events = []
    def connect(registry, servers):
        return [FakeClient(name, events) for name in servers]
    monkeypatch.setattr(registry_module, "_connect_mcp_servers", connect)
    registry = make_registry(tmp_path, mcp_servers_provider=lambda: selected)
    registry.prepare_for_run()
    original = registry._mcp_clients[0]
    original.confirmed = False
    selected["sample"] = {"command": "second"}
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        registry.prepare_for_run()
    assert registry._mcp_clients == [original]
    original.confirmed = True
    registry.prepare_for_run()
    assert original.closed
    assert registry._mcp_clients[0] is not original
    registry.close_mcp_clients()


@pytest.fixture
def plugin_setup(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_runtime
    config = SimpleNamespace(enable_plugins=True, enable_tools=True,
                             plugin_process_sandbox=False, capability_pack_mcp_enabled=False)
    events = []
    rows = [SimpleNamespace(activation_id="active-" + str(index), enabled=True,
                            manifest=SimpleNamespace(is_content_only=False, has_mcp_runtime=False)) for index in range(2)]
    class PluginClient(FakeClient):
        def __init__(self, owner, installation, *, process_sandbox, **kwargs):
            super().__init__(installation.activation_id, events)
            self.activation_ref = SimpleNamespace(scope=SimpleNamespace(activation_id=installation.activation_id),
                                                  available=lambda: True)
            self.lazy = True
            self.pack_tools = self.validated_tools = ()
            self.drift = False
            self.requested_sandbox = process_sandbox
            events.append(("construct", installation.activation_id, process_sandbox))
        def require_settled_previous_resources(self):
            pass
        def idle_stop_if_due(self, now, seconds):
            pass
    monkeypatch.setattr(plugin_runtime, "PluginMCPClient", PluginClient)
    monkeypatch.setattr(plugin_registration, "PluginMCPClient", PluginClient)
    monkeypatch.setattr(plugin_registration, "_discover_plugin_manifests", lambda owner: (tuple(rows), (1,)))
    monkeypatch.setattr(plugin_registration, "_plugin_environment_key", lambda *args: ("sw-w5-test", (True,)))
    owner = SimpleNamespace(identity=OwnerIdentity.local_main())
    registry = make_registry(tmp_path, live_config=config, plugin_owner=owner)
    return registry, config, rows, events


@pytest.mark.parametrize("key", ["enable_plugins", "enable_tools"])
def test_plugin_off_closes_and_on_reconstructs(plugin_setup, key):
    registry, config, rows, events = plugin_setup
    setattr(config, key, False)
    registry.prepare_for_run()
    assert registry._mcp_clients == []
    assert not events
    setattr(config, key, True)
    registry.prepare_for_run()
    originals = tuple(registry._mcp_clients)
    assert len(originals) == len(rows)
    setattr(config, key, False)
    registry.prepare_for_run()
    assert all(client.closed for client in originals)
    assert registry._mcp_clients == []
    setattr(config, key, True)
    registry.prepare_for_run()
    assert len(registry._mcp_clients) == len(rows)
    assert not any(client in originals for client in registry._mcp_clients)
    registry.close_mcp_clients()


def test_pack_mcp_gate_reconciles_existing_registry(plugin_setup):
    registry, config, rows, events = plugin_setup
    for row in rows:
        row.manifest.has_mcp_runtime = True
    registry.prepare_for_run()
    assert registry._mcp_clients == []
    config.capability_pack_mcp_enabled = True
    registry.prepare_for_run()
    originals = tuple(registry._mcp_clients)
    assert len(originals) == len(rows)
    config.capability_pack_mcp_enabled = False
    registry.prepare_for_run()
    assert all(client.closed for client in originals)
    assert registry._mcp_clients == []
    registry.close_mcp_clients()


def test_sandbox_flip_restarts_every_client_before_replacement(plugin_setup):
    registry, config, rows, events = plugin_setup
    registry.prepare_for_run()
    originals = tuple(registry._mcp_clients)
    events.clear()
    config.plugin_process_sandbox = True
    registry.prepare_for_run()
    assert all(client.closed for client in originals)
    assert all(client.requested_sandbox for client in registry._mcp_clients)
    assert [event[0] for event in events] == ["stop"] * len(rows) + ["construct"] * len(rows)
    current = tuple(registry._mcp_clients)
    config.plugin_process_sandbox = False
    registry.prepare_for_run()
    assert all(client.closed for client in current)
    assert all(not client.requested_sandbox for client in registry._mcp_clients)
    registry.close_mcp_clients()


def test_sandbox_cleanup_unknown_stops_sync_and_all_old_clients_are_attempted(plugin_setup):
    registry, config, rows, events = plugin_setup
    registry.prepare_for_run()
    originals = tuple(registry._mcp_clients)
    originals[0].confirmed = False
    events.clear()
    config.plugin_process_sandbox = True
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        registry.prepare_for_run()
    assert [event[0] for event in events] == ["stop"] * len(originals)
    assert all(client in originals for client in registry._mcp_clients)
    originals[0].confirmed = True
    registry.prepare_for_run()
    assert all(client.closed for client in originals)
    assert all(client.requested_sandbox for client in registry._mcp_clients)
    registry.close_mcp_clients()


@pytest.mark.parametrize("allowed", [None, ["sw_live_probe"]])
def test_fixed_deferral_never_hides_tools_without_discovery(tmp_path, allowed):
    registry = make_registry(tmp_path)
    registry.register(DeferredProbe())
    registry.disabled_tool_names.add("tool_search")
    snapshot = registry.runtime_snapshot(allowed_tools=allowed)
    assert "sw_live_probe" in {spec.name for spec in registry.model_visible_specs(runtime_snapshot=snapshot)}
    registry.close_mcp_clients()


def test_enable_tools_flip_also_syncs_fixed_server_configuration(tmp_path, monkeypatch):
    config = SimpleNamespace(enable_tools=True)
    events = []
    monkeypatch.setattr(registry_module, "_connect_mcp_servers", lambda registry, servers: [FakeClient(name, events) for name in servers])
    registry = make_registry(tmp_path, live_config=config, mcp_servers={"fixed": {"command": "not-executed"}})
    registry.prepare_for_run()
    original = registry._mcp_clients[0]
    config.enable_tools = False
    registry.prepare_for_run()
    assert original.closed and registry._mcp_clients == []
    config.enable_tools = True
    registry.prepare_for_run()
    assert registry._mcp_clients and registry._mcp_clients[0] is not original
    registry.close_mcp_clients()


def test_replacement_connection_failure_uses_initial_backoff(tmp_path, monkeypatch):
    selected = {"sample": {"command": "first"}}
    clients = []
    class UnreadyClient(FakeClient):
        def __init__(self, name):
            super().__init__(name, [])
            self.reconnect_calls = 0
        def is_running(self):
            return False
        def reconnect(self):
            self.reconnect_calls += 1
            raise AssertionError("must wait for initial backoff")
    def connect(registry, servers):
        created = [UnreadyClient(name) for name in servers]
        clients.extend(created)
        return created
    monkeypatch.setattr(registry_module.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(registry_module, "_connect_mcp_servers", connect)
    registry = make_registry(tmp_path, mcp_servers_provider=lambda: selected)
    registry.prepare_for_run()
    selected["sample"] = {"command": "second"}
    registry.prepare_for_run()
    assert len(clients) == 2
    assert all(client.reconnect_calls == 0 for client in clients)
    assert registry._mcp_retry_state[id(clients[-1])] == (1, 101.0)
    registry.close_mcp_clients()


def test_reverted_mcp_switch_still_finishes_pending_retirement(tmp_path, monkeypatch):
    selected = {"sample": {"command": "first"}}
    class PermanentClient(FakeClient):
        def stop(self):
            receipt = super().stop()
            self.closed = True
            return receipt
    monkeypatch.setattr(registry_module, "_connect_mcp_servers", lambda registry, servers: [PermanentClient(name, []) for name in servers])
    registry = make_registry(tmp_path, mcp_servers_provider=lambda: selected)
    registry.prepare_for_run()
    original = registry._mcp_clients[0]
    original.confirmed = False
    selected["sample"] = {"command": "second"}
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        registry.prepare_for_run()
    original.confirmed = True
    selected["sample"] = {"command": "first"}
    registry.prepare_for_run()
    assert original not in registry._mcp_clients
    assert registry._mcp_clients and registry._mcp_clients[0].is_running()
    registry.close_mcp_clients()


def test_reverted_sandbox_switch_still_finishes_pending_retirement(plugin_setup):
    registry, config, rows, events = plugin_setup
    registry.prepare_for_run()
    originals = tuple(registry._mcp_clients)
    original = originals[0]
    stop = original.stop
    def permanent_stop():
        receipt = stop()
        original.closed = True
        return receipt
    original.stop = permanent_stop
    original.confirmed = False
    config.plugin_process_sandbox = True
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        registry.prepare_for_run()
    original.confirmed = True
    config.plugin_process_sandbox = False
    registry.prepare_for_run()
    assert all(client not in originals for client in registry._mcp_clients)
    assert all(client.is_running() for client in registry._mcp_clients)
    registry.close_mcp_clients()


def test_missing_replacement_clients_are_retried_on_next_run(tmp_path, monkeypatch):
    selected = {"sample": {"command": "first"}}
    calls = []
    def connect(registry, servers):
        calls.append(tuple(servers))
        if len(calls) == 2:
            return []
        return [FakeClient(name, []) for name in servers]
    monkeypatch.setattr(registry_module, "_connect_mcp_servers", connect)
    registry = make_registry(tmp_path, mcp_servers_provider=lambda: selected)
    registry.prepare_for_run()
    selected["sample"] = {"command": "second"}
    registry.prepare_for_run()
    assert registry._mcp_clients == []
    registry.prepare_for_run()
    assert len(calls) == 3 and registry._mcp_clients
    registry.close_mcp_clients()


def test_nested_mcp_configuration_does_not_mutate_applied_snapshot(tmp_path, monkeypatch):
    selected = {"sample": {"command": "fixed", "args": ["first"]}}
    monkeypatch.setattr(registry_module, "_connect_mcp_servers", lambda registry, servers: [FakeClient(name, []) for name in servers])
    registry = make_registry(tmp_path, mcp_servers_provider=lambda: selected)
    registry.prepare_for_run()
    original = registry._mcp_clients[0]
    selected["sample"]["args"][0] = "second"
    assert registry._mcp_servers_state[0]["sample"]["args"] == ["first"]
    registry.prepare_for_run()
    assert original.closed and registry._mcp_clients[0] is not original
    registry.close_mcp_clients()


def test_unstarted_plugin_check_failure_has_confirmed_cleanup(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from agent_py_agent.agent import plugin_runtime
    from agent_py_agent.agent.tooling.mcp_client import MCPServerConfig, MCPStdioClient

    # 与真实 PluginMCPClient 构造末尾相同的基类状态；不调用 start、不构造替身 stop。
    client = object.__new__(plugin_runtime.PluginMCPClient)
    MCPStdioClient.__init__(client, MCPServerConfig(name="unstarted-plugin", command="not-executed"))
    client.activation_ref = SimpleNamespace(owner=lambda: SimpleNamespace(home_dir=tmp_path))
    transaction = SimpleNamespace(list_records=lambda: ([], ["unreadable"]))
    monkeypatch.setattr(plugin_runtime, "ProcessSessionStore", lambda root: SimpleNamespace(
        transaction=lambda: nullcontext(transaction)))
    with pytest.raises(ValueError, match="原资源目录不可读"):
        client.require_settled_previous_resources()
    assert client._transport is None and client._starting is False and client._launch_failure is None
    receipt = client.stop()
    assert receipt.confirmed is True
    assert client.is_closed() is True
