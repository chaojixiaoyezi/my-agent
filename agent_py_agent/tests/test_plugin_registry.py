"""新运行插件组合、同 owner 视图与关闭交错，不代替真实 TUI 验收。"""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_runtime import PluginMCPClient, plugin_tool_name
from agent_py_agent.agent.tooling import plugin_registration
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.tests.plugin_activation_fixtures import (
    installed_runtime_plugin,
    plugin_registry,
)


@pytest.mark.parametrize("phase", ["construct", "resources"])
def test_close_collects_client_registered_after_first_close_snapshot(tmp_path, monkeypatch, phase):
    service = installed_runtime_plugin(tmp_path)
    enabled = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="enable")
    assert enabled["state"] == "succeeded", enabled
    registry = plugin_registry(service)
    view = registry.with_access_policy(access_mode="full-access", path_access_mode="full", owner_scope_root="")
    entered, release, created = threading.Event(), threading.Event(), []
    original = PluginMCPClient.__init__
    resources = PluginMCPClient.require_settled_previous_resources

    def gated_constructor(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)
        if phase == "construct":
            entered.set()
            assert release.wait(10)

    def gated_resources(client):
        resources(client)
        if phase == "resources":
            entered.set()
            assert release.wait(10)

    monkeypatch.setattr(PluginMCPClient, "__init__", gated_constructor)
    monkeypatch.setattr(PluginMCPClient, "require_settled_previous_resources", gated_resources)
    with ThreadPoolExecutor(max_workers=2) as pool:
        preparing = pool.submit(view.prepare_for_run)
        assert entered.wait(5)
        closing = pool.submit(registry.close_mcp_clients)
        try:
            assert registry._mcp_closed.wait(3)
        finally:
            release.set()
        preparing.result(timeout=10)
        closing.result(timeout=10)
    assert len(created) == 1 and created[0].is_closed() and created[0]._transport is None
    assert not registry._mcp_clients and not view._mcp_clients
    view.prepare_for_run()
    assert len(created) == 1 and plugin_tool_name("sample-peek", "read") not in view.tools


def test_private_owner_stays_fixed_across_full_access_view(tmp_path):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    service = installed_runtime_plugin(tmp_path)
    owner = resolve_owner_home(service.context.owner.root, OwnerIdentity.provider_user("test", "other"))
    registry = plugin_registry(service, plugin_owner=owner, owner_scope_root=str(owner.home_dir))
    try:
        view = registry.with_access_policy(access_mode="full-access", path_access_mode="full", owner_scope_root="")
        assert view._construction_params.plugin_owner == owner
        view.prepare_for_run()
        assert not view._mcp_clients
        assert not owner.plugins_dir.exists()
    finally:
        registry.close_mcp_clients()


def test_new_registry_does_not_bypass_persisted_cleanup_unknown(tmp_path, monkeypatch):
    service = installed_runtime_plugin(tmp_path)
    enabled = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="enable")
    assert enabled["state"] == "succeeded", enabled
    owner = service.context.owner
    store = ProcessSessionStore(process_session_store_root(owner.home_dir, owner.home_dir))
    records, _ = store.list_records()
    candidate = next(row for row in records if row["activation_scope"])
    # 以新的固定资源记录表示同代未确认退出；不改写既有成功清理历史。
    unresolved = {**candidate, "session_id": "bg-unresolved", "revision": 0, "status": "unknown",
                  "finished_at": None, "exit_code": None, "termination": {"cleanup": {"confirmed": False, "instances": []}}}
    store.write(unresolved)
    original, created = PluginMCPClient.__init__, []

    def capture_client(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(PluginMCPClient, "__init__", capture_client)
    registry = plugin_registry(service)
    try:
        registry.prepare_for_run()
        assert not registry._mcp_clients
        assert len(created) == 1 and created[0].is_closed() and created[0]._transport is None
        assert len(store.list_records()[0]) == len(records) + 1
        assert "read_file" in registry.tools
    finally:
        registry.close_mcp_clients()


def test_plugins_disabled_has_no_owner_read_or_process(tmp_path, monkeypatch):
    service = installed_runtime_plugin(tmp_path)
    registry = plugin_registry(service, plugin_owner=None)
    monkeypatch.setattr(plugin_registration.PluginInstallStore, "snapshot", lambda *_: pytest.fail("disabled plugin read"))
    try:
        registry.prepare_for_run()
        assert registry._construction_params.plugin_owner is None and not registry._mcp_clients
        assert replace(registry._construction_params, path_access_mode="full").plugin_owner is None
    finally:
        registry.close_mcp_clients()


@pytest.mark.parametrize("plugins,tools", [(True, True), (False, True), (True, False)])
def test_core_scoped_owner_injection_is_lazy_and_obeys_flags(tmp_path, monkeypatch, plugins, tools):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.owner_scoped_pool import _config_with_owner
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    identity = OwnerIdentity.provider_user("test", "scoped-worker")
    config = _config_with_owner(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                          enable_plugins=plugins, enable_tools=tools), identity)
    monkeypatch.setattr(PluginMCPClient, "start", lambda *_: pytest.fail("Agent construction started plugin"))
    agent = SimpleAgent(config, tmp_path)
    try:
        injected = agent.tools._construction_params.plugin_owner
        assert injected == (resolve_owner_home(agent.home_paths.root, identity) if plugins and tools else None)
        assert not agent.tools._mcp_clients
    finally:
        agent.tools.close_mcp_clients()
