"""W2 开关是用户安全边界；关闭与非 main 均先于候选、CAS 和环境副作用。"""
from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace

import pytest
import yaml

from agent_py_agent.agent.plugin_enable_tool import PluginEnablePolicy, PluginEnableTool
from agent_py_agent.agent.plugin_management import plugin_management_context
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.tests.test_cpack_w2_enable_pin_catalog import _build, _enable, _service
from agent_py_agent.tests.test_plugin_management import manager


def test_config_defaults_yaml_range_and_user_boundary(tmp_path):
    from pathlib import Path

    from agent_py_agent.agent.settings.parameter_registry import _BOUNDARY_NAMES
    from agent_py_agent.agent.settings.user_config_capability import USER_SETTINGS_BOUNDARY_KEYS

    config = AgentConfig()
    assert getattr(config, "capability_pack_mcp_enabled", None) is False
    assert getattr(config, "capability_pack_mcp_idle_stop_seconds", None) == 600
    defaults = yaml.safe_load((Path(__file__).parents[1] / "config/agent_config.yaml").read_text())
    assert defaults["capability_pack_mcp_enabled"] is False
    assert defaults["capability_pack_mcp_idle_stop_seconds"] == 600
    for name in ("capability_pack_mcp_enabled", "capability_pack_mcp_idle_stop_seconds"):
        assert name in _BOUNDARY_NAMES and name in USER_SETTINGS_BOUNDARY_KEYS
    for value, expected in ((0, 0), (11, 11), (-1, 600), ("bad", 600)):
        path = tmp_path / "agent.yaml"
        path.write_text(yaml.safe_dump({"capability_pack_mcp_idle_stop_seconds": value}))
        assert load_config(path).capability_pack_mcp_idle_stop_seconds == expected


@pytest.mark.parametrize("switch", [True, False, "false"])
def test_management_context_reads_only_true_and_carries_idle_stop(tmp_path, switch):
    service, _ = manager(tmp_path)
    home = home_paths_with_owner(home_paths(service.context.owner.root), service.context.owner)
    config = AgentConfig()
    config.capability_pack_mcp_enabled = switch
    config.capability_pack_mcp_idle_stop_seconds = 42
    context = plugin_management_context(service.context.owner, home, config, service.context.threads,
        actor_id="tester", channel="chat", conversation_id="session", is_admin=True)
    assert getattr(context, "capability_pack_mcp_enabled", None) is (switch is True)
    assert context.capability_pack_mcp_idle_stop_seconds == 42
    from agent_py_agent.agent.plugin_management import _enable_policy

    policy = _enable_policy(context, {})
    assert policy.pack_mcp_enabled is (switch is True) and policy.pack_mcp_idle_stop_seconds == 42


def test_switch_off_has_not_started_and_no_cas_environment_or_launch(tmp_path, monkeypatch):
    service, entry = _service(tmp_path, switch=False)
    def unexpected(*args, **kwargs):
        pytest.fail("关闭时不允许构造计划、CAS、准备环境或启动候选")
    from agent_py_agent.agent import plugin_enable_tool

    for name in ("plan_plugin_environment", "prepare_plugin_environment", "PluginMCPClient"):
        monkeypatch.setattr(plugin_enable_tool, name, unexpected)
    monkeypatch.setattr(type(service.installations), "change_activation", unexpected)
    result = _enable(service)
    assert result["error_code"] == "PACK_MCP_DISABLED", result
    # HostCommand 查询投影不公开 effect_outcome；在真实 handler 返回口核对同一开关事实，不给 wire 加键。
    tool = PluginEnableTool(service.context.owner, None,
        SimpleNamespace(request=SimpleNamespace(operation_id="closed-pack")), entry, "fixed",
        policy=PluginEnablePolicy(pack_mcp_enabled=False))
    assert tool.execute({"plugin": "w2-pack", "catalog_revision": "fixed"}).effect_outcome == "not_started"
    assert service.installations.snapshot() == (entry,)
    assert not (service.context.owner.plugins_dir / "environments").exists()


def test_non_main_owner_refused_before_runtime_and_sandbox_checks(tmp_path, monkeypatch):
    assert "pack_mcp_enabled" in {item.name for item in fields(PluginEnablePolicy)}
    service, _ = manager(tmp_path)
    from agent_py_agent.agent.plugin_installation import PluginInstallRequest

    other = resolve_owner_home(service.context.owner.root, OwnerIdentity("local", "user", "other"))
    from agent_py_agent.agent.plugin_install_store import PluginInstallStore

    store = PluginInstallStore(other)
    entry = store.install(PluginInstallRequest(_build(), "install-other", 0)).installation
    from agent_py_agent.agent import plugin_enable_tool

    def unexpected(*args, **kwargs):
        pytest.fail("不支持 owner 不能解析运行时、构造计划或检查沙箱")
    monkeypatch.setattr(plugin_enable_tool, "_runtime_facts", unexpected)
    monkeypatch.setattr(plugin_enable_tool, "_sandbox_problem", unexpected)
    tool = PluginEnableTool(other, None, SimpleNamespace(request=SimpleNamespace(operation_id="enable-other")),
        entry, "revision", policy=PluginEnablePolicy(pack_mcp_enabled=True))
    result = tool.execute({"plugin": "w2-pack", "catalog_revision": "revision"})
    assert result.error_code == "PACK_MCP_OWNER_UNSUPPORTED" and result.effect_outcome == "not_started"
    assert store.snapshot() == (entry,)


def test_pack_does_not_need_event_switch_but_still_forces_sandbox(tmp_path):
    service, _ = _service(tmp_path)
    assert service.context.plugin_events_enabled is False
    reply = _enable(service)
    assert reply["details"]["reason"] == "confirmation_required", reply
    assert reply["details"]["confirmation"]["sandbox"] == "required"
    assert "network" in reply["details"]["confirmation"]


def test_context_positional_contract_and_policy_keyword_only_fields():
    from agent_py_agent.agent.plugin_management import PluginManagementContext

    context_fields = [item.name for item in fields(PluginManagementContext) if not item.kw_only]
    assert context_fields[-3:] == ["approval_timeout_seconds", "capability_pack_mcp_enabled", "capability_pack_mcp_idle_stop_seconds"]
    by_name = {item.name: item for item in fields(PluginEnablePolicy)}
    assert by_name["pack_mcp_enabled"].kw_only and by_name["pack_mcp_idle_stop_seconds"].kw_only
