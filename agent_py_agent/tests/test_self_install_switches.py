"""learnpack 第 1 步：「能力包自动装」「插件自动装」两个开关。

锁定：默认都关（dataclass 与随包 YAML 一致）；参数中心登记成管理员专用边界项、生效时机如实报"马上生效"；
模型改不了、管理员 /settings 能改（TUI 文本回放、IM 绑定管理员各一条，非管理员被拒）；运行时每次按文件现读，
不吃 agent 上缓存的旧快照；配置读不到按"关"；回执给出的开关命令能被 /settings 解析器原样接受；模型能只读查看。
"""
from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.capability.runtime_config_reload import (
    bundled_capability_config_path,
    capability_config_version,
    load_capability_config_snapshot,
)
from agent_py_agent.agent.capability.self_install_switches import (
    PACK_SELF_INSTALL_KEY,
    PLUGIN_SELF_INSTALL_KEY,
    SELF_INSTALL_SWITCH_KEYS,
    UNREADABLE_CONFIG_VERSION,
    read_self_install_switches,
    switch_command,
    switch_facts,
)
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import settings_control_service
from agent_py_agent.agent.settings.parameter_changes import (
    ChangeOrigin,
    WritePaths,
    set_parameter,
    user_settings_write_scope,
)
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.settings.user_config_capability import (
    BOUNDARY_KEYS,
    EFFECT_IMMEDIATE,
    USER_SETTINGS_BOUNDARY_KEYS,
)
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.cli.chat_parts import control_runtime

_ADMIN = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main")
_FEISHU_USER = SimpleNamespace(owner_provider="feishu", owner_kind="users", owner_id="ou_x")
_KEYS = (PACK_SELF_INSTALL_KEY, PLUGIN_SELF_INSTALL_KEY)


def _agent(tmp_path, *, home=_ADMIN):
    user_config = tmp_path / "desktop.yaml"
    if not user_config.exists():
        user_config.write_text('agent_name: "myagent"\n', encoding="utf-8")
    capability = tmp_path / "owner" / "config" / "capability_config.yaml"
    return SimpleNamespace(
        config=SimpleNamespace(config_path=str(user_config)), root=tmp_path / "owner",
        capability_config_path=str(capability), home_paths=home,
    )


def _write(agent, text: str) -> None:
    path = os.fspath(agent.capability_config_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def test_both_switches_default_off_in_dataclass_and_packaged_yaml():
    packaged = load_capability_config(bundled_capability_config_path())
    for key in _KEYS:
        assert getattr(CapabilityConfig(), key) is False
        assert getattr(packaged, key) is False
    assert frozenset(_KEYS) == SELF_INSTALL_SWITCH_KEYS


@pytest.mark.parametrize("key", _KEYS)
def test_registry_marks_switch_admin_only_and_immediate(key):
    spec = parameter_registry()[key]
    assert (spec.source, spec.default, spec.writable, spec.effect) == ("capability", False, False, EFFECT_IMMEDIATE)
    assert key in USER_SETTINGS_BOUNDARY_KEYS and "模型不能改" in BOUNDARY_KEYS[key]
    assert "马上生效" in spec.description and "/settings" in spec.description


@pytest.mark.parametrize("key", _KEYS)
def test_model_is_refused_but_admin_settings_write_takes_effect_now(tmp_path, key):
    agent = _agent(tmp_path)
    paths = WritePaths(user_path=agent.config.config_path, capability_path=agent.capability_config_path)
    refused = set_parameter(key, True, paths=paths, origin=ChangeOrigin("model"))
    assert refused["ok"] is False and refused["code"] == "PARAMETER_BOUNDARY"
    assert not os.path.exists(agent.capability_config_path)
    with user_settings_write_scope():
        report = set_parameter(key, "开", paths=paths, origin=ChangeOrigin("chat"))
    assert report["ok"] is True and report["effect_when"] == EFFECT_IMMEDIATE and "已经生效" in report["note"]
    assert stat.S_IMODE(os.stat(agent.capability_config_path).st_mode) == 0o600
    switches = read_self_install_switches(agent)
    assert (switches.capability_pack, switches.plugin) == (key == PACK_SELF_INSTALL_KEY, key == PLUGIN_SELF_INSTALL_KEY)


def test_switch_is_read_fresh_even_with_a_cached_snapshot(tmp_path):
    agent = _agent(tmp_path)
    _write(agent, "capability_pack_self_install_enabled: false\n")
    # Gateway 启动时缓存的旧快照：开关读取不能吃这份缓存，否则 /settings 改完要等重启。
    agent._capability_config_runtime_snapshot = load_capability_config_snapshot(agent.capability_config_path)
    _write(agent, "capability_pack_self_install_enabled: true\nplugin_self_install_enabled: true\n")
    switches = read_self_install_switches(agent)
    assert (switches.capability_pack, switches.plugin) == (True, True)
    assert switches.config_version == capability_config_version(agent.capability_config_path)
    _write(agent, "capability_pack_self_install_enabled: false\nplugin_self_install_enabled: true\n")
    assert (read_self_install_switches(agent).capability_pack, read_self_install_switches(agent).plugin) == (False, True)
    assert agent._capability_config_runtime_snapshot.config.capability_pack_self_install_enabled is False


def test_missing_user_file_reads_packaged_default_and_unreadable_reads_off(tmp_path):
    agent = _agent(tmp_path)
    missing = read_self_install_switches(agent)
    assert (missing.capability_pack, missing.plugin) == (False, False)
    assert missing.config_version == capability_config_version(bundled_capability_config_path())
    os.makedirs(agent.capability_config_path)  # 路径被目录占住：读不出来就按关处理
    broken = read_self_install_switches(agent)
    assert (broken.capability_pack, broken.plugin, broken.config_version) == (False, False, UNREADABLE_CONFIG_VERSION)


def test_switch_facts_commands_round_trip_through_the_settings_parser():
    facts = switch_facts(read_self_install_switches(SimpleNamespace(root="/nonexistent", capability_config_path="")))
    commands = facts["switch_commands"]
    assert set(commands) == {"capability_pack_on", "capability_pack_off", "plugin_on", "plugin_off"}
    for text in commands.values():
        command = parse_conversation_control(text, reject_unknown_slash=True)
        assert (command.kind, command.operation, command.valid) == ("settings", "set", True)
        assert control_runtime._command_text(command) == text  # TUI 发给 Gateway 的文字原样还原
    assert commands["plugin_off"] == switch_command(PLUGIN_SELF_INSTALL_KEY, False) == (
        "/settings set plugin_self_install_enabled false")
    assert facts[PACK_SELF_INSTALL_KEY] is False and facts[PLUGIN_SELF_INSTALL_KEY] is False


def _settings_run(monkeypatch, agent, text, *, home):
    monkeypatch.setattr(settings_control_service, "_scoped_home", lambda _agent, _scope: home)
    command = parse_conversation_control(text, reject_unknown_slash=True)
    return settings_control_service.execute_settings_control(agent, command, None)


# TUI（本机管理员）与 IM（已用 /admin 绑定的管理员私聊，解析出的也是本机管理员身份）走同一个 Gateway /settings 服务。
@pytest.mark.parametrize("channel_home", [_ADMIN, SimpleNamespace(owner_provider="local", owner_kind="main",
                                                                   owner_id="main", channel="feishu")])
def test_admin_turns_switch_on_and_off_through_gateway_settings(monkeypatch, tmp_path, channel_home):
    agent = _agent(tmp_path)
    turned_on = _settings_run(monkeypatch, agent, "/settings set plugin_self_install_enabled true", home=channel_home)
    assert turned_on.ok and "马上生效" in turned_on.message and "/restart" not in turned_on.message
    assert read_self_install_switches(agent).plugin is True
    turned_off = _settings_run(monkeypatch, agent, "/settings set plugin_self_install_enabled false", home=channel_home)
    assert turned_off.ok and read_self_install_switches(agent).plugin is False


def test_feishu_non_admin_cannot_turn_switch_on(monkeypatch, tmp_path):
    agent = _agent(tmp_path)
    refused = _settings_run(monkeypatch, agent, "/settings set capability_pack_self_install_enabled true",
                            home=_FEISHU_USER)
    assert refused.ok is False and "管理员" in refused.message
    assert not os.path.exists(agent.capability_config_path)


def test_model_can_view_switch_value_read_only(tmp_path):
    agent = _agent(tmp_path)
    _write(agent, "plugin_self_install_enabled: true\n")
    outcome = UserConfigTool(agent).execute({"action": "view", "key": PLUGIN_SELF_INSTALL_KEY})
    parameter = json.loads(outcome.output)["parameter"]
    assert outcome.ok and parameter["running_value"] == "true" and parameter["writable"] is False
    assert parameter["effect_when"] == EFFECT_IMMEDIATE
