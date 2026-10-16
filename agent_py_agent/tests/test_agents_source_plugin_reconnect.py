"""插件真实构造/重连的来源profile；激活与MCP传输替代，不是OS或真实握手验收。"""
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import plugin_runtime as plugin
from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox
from agent_py_agent.agent.capability.agents_source_contract import AgentsContractError
from agent_py_agent.agent.capability.agents_source_repository import AgentsSourceRepository
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt
from agent_py_agent.tests.test_agents_source_protection import protected as protected
from agent_py_agent.tests.test_agents_source_repository import choose


@pytest.fixture
def plugin_client(protected, monkeypatch):
    p = protected
    owner = SimpleNamespace(root=p.root, home_dir=p.home, plugins_dir=p.home / "data/plugins")
    environment = owner.plugins_dir / "environments/fixture"
    environment.mkdir(parents=True)
    manifest = SimpleNamespace(plugin_id="source-reconnect", permissions=SimpleNamespace(network=False),
                               settings_schema={}, host_api=(), tools=(), is_content_only=False)
    activation = SimpleNamespace(phase="active", activation_id="fixture", plan=SimpleNamespace(
        environment_ref="fixture", interpreter_fingerprint="fixture"))
    installation = SimpleNamespace(manifest=manifest, activation=activation, settings_json="{}")
    ref = SimpleNamespace(require=lambda **kw: installation, owner=lambda: owner)
    monkeypatch.setattr(plugin.PluginActivationRef, "from_owner", lambda *a: ref)
    monkeypatch.setattr(plugin, "_launch_argv", lambda *a: (sys.executable, ["-c", "pass"], environment))
    monkeypatch.setattr(plugin, "_plugin_v8_sandbox", lambda *a: None)
    launched, profiles = [], []

    # LLM: 仅检验Seatbelt规则文本及插件重连调用链；显式注入未执行的路径，不依赖测试主机安装macOS命令。
    # 函数用途: 在所有平台生成同一保护规则投影，保留后续全部规则与生命周期断言。
    def projection(argv, spec):
        from dataclasses import replace

        actual = AttemptExecutionSandbox(replace(spec, macos_sandbox_exec="test-sandbox-exec"))._macos_argv(argv)
        profile = actual[actual.index("-p") + 1]
        profiles.append(profile)
        return ["test-platform-wrapper", profile, *argv]

    def start_transport(client):
        closed = threading.Event()
        transport = SimpleNamespace(inbox=SimpleNamespace(closed=closed), ready=True,
                                    is_running=lambda: not closed.is_set(), revoke=closed.set,
                                    terminate=lambda **kw: ProcessTerminationReceipt("test-transport", True, 0, 1))
        client._transport, client._starting = transport, False
        launched.append((client.config.command, *client.config.args))
        return transport

    monkeypatch.setattr(plugin, "sandboxed_plugin_argv", projection)
    monkeypatch.setattr(MCPStdioClient, "_start_transport", start_transport)
    client = plugin.PluginMCPClient(owner, installation, process_sandbox=True)
    repo = AgentsSourceRepository(owner="local/main", owner_home=p.home,
                                 path_policy=PathAccessPolicy.from_values(mode="full", agent_home_root=p.root))
    other = p.home / "docs/next.data"
    other.write_text("# next\n- 保留用户原件。\n")
    try:
        yield client, repo, other, launched, profiles
    finally:
        client.stop()


def test_first_plugin_start_rebuilds_projection_after_constructor_source_change(protected, plugin_client):
    client, repo, other, launched, _ = plugin_client
    choose(repo, other)
    client.start()
    assert f"(literal {json.dumps(str(other))})" in launched[-1][1], launched
    assert launched[-1].count("test-platform-wrapper") == 1


def test_plugin_reconnect_rebuilds_new_source_and_never_wraps_old_wrapper(protected, plugin_client):
    client, repo, other, launched, _ = plugin_client
    first = client.start()
    first.revoke()
    choose(repo, other)
    second = client.reconnect()
    assert second is not first
    assert f"(literal {json.dumps(str(other))})" in launched[-1][1], launched
    assert launched[-1].count("test-platform-wrapper") == 1
    assert protected.source.read_bytes() == protected.before


def test_live_plugin_is_retired_before_source_commit(protected, plugin_client):
    client, repo, other, _, _ = plugin_client
    first = client.start()
    choose(repo, other)
    assert first.inbox.closed.is_set(), "切源不能留原连接继续执行"
    assert client.reconnect() is not first


def test_unconfirmed_plugin_retirement_keeps_original_configuration(protected, plugin_client):
    client, repo, other, launched, _ = plugin_client
    first = client.start()
    first.terminate = lambda **kw: ProcessTerminationReceipt("test-unconfirmed", False, None, 1)
    with pytest.raises(AgentsContractError, match="AGENTS_SOURCE_RESOURCE_UNCONFIRMED"):
        choose(repo, other)
    assert protected.control.read_bytes() == protected.config_before
    assert len(launched) == 1


def test_repeated_start_does_not_replace_running_transport_or_rebuild_profile(plugin_client):
    client, _, _, launched, profiles = plugin_client
    first = client.start()
    count = len(profiles)
    assert client.start() is first
    assert len(profiles) == count and len(launched) == 1
