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
from agent_py_agent.tests.plugin_enable_fixtures import (
    enable_with_confirmation,
    unrestricted_service,
)


@pytest.mark.parametrize("phase", ["construct", "resources"])
def test_close_collects_client_registered_after_first_close_snapshot(tmp_path, monkeypatch, phase):
    service = unrestricted_service(installed_runtime_plugin(tmp_path))
    enable_with_confirmation(service, "sample-peek")
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
        closing = pool.submit(lambda: (registry.close_mcp_clients(), view.close_mcp_clients()))
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
    service = unrestricted_service(installed_runtime_plugin(tmp_path))
    enable_with_confirmation(service, "sample-peek")
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


# LLM: 使用真实安装/激活领域记录，传输停止回执为合成输入；不运行原生进程、不把纯 CAS 当作退出证明。
# 函数用途: 复现管理端消费原进程记录后，Registry 仍持有旧客户端的衔接状态。
@pytest.fixture
def released_client_registry(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.plugin_activation import prepare_release
    from agent_py_agent.agent.tooling.process_scope import ProcessActivationScope
    from agent_py_agent.agent.tooling.process_session_cleanup import ProcessSessionCleanup
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
    from agent_py_agent.tests.test_plugin_activation import (
        activation_fixture,
        publication,
        revocation,
    )

    store, _, request = activation_fixture(tmp_path)
    active = store.change_activation(publication(store.change_activation(request))).installation
    revoked = store.change_activation(revocation(active)).installation
    released = prepare_release("disable-a", revoked, (revoked,)).installation
    owner = resolve_owner_home(tmp_path / "home")
    rows = [released]
    created = []

    class Client:
        def __init__(self, owner, installation, **kwargs):
            self.installation = installation
            self.activation_ref = SimpleNamespace(scope=ProcessActivationScope(
                owner.owner_id, str(owner.home_dir), installation.manifest.plugin_id, installation.activation_id))
            self._registry_process_sandbox = False
            self.receipt = ProcessSessionCleanup({"session_id": "bg-original"}, False, authority_missing=True)
            self.stops = 0
            created.append(self)

        def stop(self):
            self.stops += 1
            return self.receipt

        def require_settled_previous_resources(self):
            pass

    monkeypatch.setattr(plugin_registration, "PluginMCPClient", Client)
    monkeypatch.setattr(plugin_registration, "_discover_plugin_manifests", lambda _: (tuple(rows), ()))
    client = Client(owner, active)
    registry = SimpleNamespace(
        _plugin_owner=owner, plugins_enabled=True, plugin_process_sandbox=False,
        capability_pack_mcp_enabled=True, _mcp_clients=[client], _mcp_retry_state={id(client): object()},
        _mcp_closed=threading.Event(), tools={"read_file": object()},
        _construction_params=SimpleNamespace(plugin_owner=owner, host_credential_env_names=frozenset()),
    )
    return registry, rows, client, active, revoked, released, created


# LLM: 两个权限视图共享同一客户端池；释放回执必须精确对应旧代，且原未确认停止回执不能被改写。
# 函数用途: 验证已消费旧资源不会永远挡住后来新代，核心工具和原停止事实保持不变。
def test_released_client_with_consumed_record_does_not_block_next_generation(released_client_registry):
    from copy import copy

    from agent_py_agent.agent.plugin_activation import PluginActivationRequest, prepare_activation
    from agent_py_agent.agent.plugin_activation_record import PluginActivation
    from agent_py_agent.tests.test_plugin_activation import publication

    registry, rows, client, active, _, released, created = released_client_registry
    view = copy(registry)
    core = registry.tools["read_file"]
    plugin_registration.synchronize_plugin_clients(view)
    plugin_registration.synchronize_plugin_clients(registry)
    assert registry._mcp_clients == [] and view._mcp_clients is registry._mcp_clients
    assert client.stops == 1 and not registry._mcp_retry_state
    assert client.receipt.authority_missing and not client.receipt.confirmed
    plan = replace(active.activation.plan, operation_id="enable-b", installation_revision=released.revision)
    prepared = prepare_activation(PluginActivationRequest("enable-b", released.revision,
        PluginActivation(plan, "preparing")), (released,))
    current = prepare_activation(publication(prepared), (prepared.installation,)).installation
    rows[:] = [current]
    plugin_registration.synchronize_plugin_clients(registry)
    assert registry._mcp_clients == [created[-1]] and len(created) == 2
    assert created[-1].installation.activation_id == current.activation_id != active.activation_id
    assert registry.tools["read_file"] is core and client.stops == 1


# LLM: 缺记录不等于退出；只有精确原代 release 且 stop 明确报告 authority_missing 才可移交责任。
# 函数用途: 验证未知停止、仅撤销、缺安装及其它代释放仍保留旧客户端，阻止后续替代启动。
@pytest.mark.parametrize("case", ["revoked", "missing", "other_generation", "unknown", "launch_pending"])
def test_unconfirmed_client_without_exact_release_keeps_responsibility(released_client_registry, case):
    from agent_py_agent.agent.plugin_installation import plugin_input_digest
    from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt
    from agent_py_agent.agent.tooling.process_session_cleanup import ProcessSessionCleanup

    registry, rows, client, active, revoked, released, created = released_client_registry
    if case == "revoked":
        rows[:] = [revoked]
    elif case == "missing":
        rows[:] = []
    elif case == "other_generation":
        old = released.last_commit
        foreign = "b" * 64
        receipt = replace(old, activation_sha256=foreign, input_digest=plugin_input_digest(
            "release", old.plugin_id, old.package_sha256, old.before_revision, activation_sha256=foreign))
        rows[:] = [replace(released, last_commit=receipt)]
    elif case == "unknown":
        client.receipt = ProcessSessionCleanup({"session_id": "bg-original"}, False)
    else:
        client.receipt = ProcessTerminationReceipt("launch_pending", False, None, 0)
    plugin_registration.synchronize_plugin_clients(registry)
    assert registry._mcp_clients == [client] and client._registry_policy_retiring
    assert client.stops == 1 and not registry._mcp_retry_state
    rows[:] = [active]
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        plugin_registration.synchronize_plugin_clients(registry)
    assert registry._mcp_clients == [client] and len(created) == 1 and client.stops == 2
