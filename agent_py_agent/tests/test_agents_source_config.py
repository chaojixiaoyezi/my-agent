# LLM: 仅验证配置的默认、归一、加载失败与展示目录；不模拟来源内容加载或权限审批。
# 模块用途: 锁定 AGENTS 两个开关的配置合同，供父级接回原 tests 目录后运行。
from __future__ import annotations

import json
import socket
import urllib.request
from dataclasses import fields
from pathlib import Path

import pytest

from agent_py_agent.agent.settings import config as config_module
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.config_io import load_simple_yaml, parse_scalar
from agent_py_agent.agent.settings.normalize import normalize_agent_config
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.settings.user_config_capability import EFFECT_GATEWAY_RESTART

_PACKAGE = Path(config_module.__file__).resolve().parents[2]
_PACKAGED = _PACKAGE / "config" / "agent_config.yaml"
_SWITCHES = {"agents_source_enabled": True, "agents_source_network_enabled": False}


# LLM: 使用已有随包文件走正式加载入口，只替换磁盘解析结果；不写用户 home 或配置文件。
# 函数用途: 在不创建真实部署配置的情况下测试 loader 对原始配置字典的处理。
def _load_raw(monkeypatch, raw):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.setattr(config_module, "load_simple_yaml", lambda _path: dict(raw))
    return load_config(_PACKAGED)


# LLM: bool 必须是真布尔，不允许真假相等的整数冒充 schema 默认。
# 函数用途: 保留默认 owner AGENTS 行为，并让网络加载默认关闭。
@pytest.mark.parametrize("key,expected", _SWITCHES.items())
def test_switch_defaults_are_real_booleans(key, expected):
    value = getattr(AgentConfig(), key)
    assert type(value) is bool
    assert value is expected


# LLM: 同时核对显式 YAML 键、原始类型和正式加载值，避免只靠 dataclass 缺省假装 parity。
# 函数用途: 验证随包配置与两个开关的源 schema 默认一致。
@pytest.mark.parametrize("key,expected", _SWITCHES.items())
def test_packaged_switch_defaults_match_schema(monkeypatch, key, expected):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    raw = load_simple_yaml(_PACKAGED)
    assert type(raw[key]) is bool
    assert raw[key] is expected
    assert getattr(load_config(_PACKAGED), key) is expected


# LLM: 旧部署没有这两个键时只能使用 schema 默认，不能推导任何新网络来源。
# 函数用途: 验证缺省键仍保留 owner 默认与网络关闭。
def test_legacy_config_without_switches_uses_defaults(monkeypatch):
    loaded = _load_raw(monkeypatch, {})
    for key, expected in _SWITCHES.items():
        assert getattr(loaded, key) is expected


# LLM: 引号 false 是文本而不是真值许可；共用正规解析与布尔归一，不增加隐含开启行为。
# 函数用途: 验证两个开关可由显式布尔或带引号布尔配置安全修改。
@pytest.mark.parametrize("key", _SWITCHES)
@pytest.mark.parametrize("raw,expected", [(True, True), (False, False), ('"true"', True), ('"false"', False)])
def test_explicit_switch_values_are_normalized(monkeypatch, key, raw, expected):
    parsed = parse_scalar(raw) if isinstance(raw, str) else raw
    assert getattr(_load_raw(monkeypatch, {key: parsed}), key) is expected


# LLM: 无效配置回落 schema 默认并告警；尤其 network 不能因非空字符串而意外开启。
# 函数用途: 验证非法开关值无法作为真值进入加载后的运行配置。
@pytest.mark.parametrize("key,expected", _SWITCHES.items())
@pytest.mark.parametrize("raw", ["not-a-boolean", 2, [], {}])
def test_invalid_switch_values_fall_back_and_warn(monkeypatch, key, expected, raw):
    loaded = _load_raw(monkeypatch, {key: raw})
    assert getattr(loaded, key) is expected
    assert any(key in warning and "boolean" in warning for warning in loaded.config_warnings)


# LLM: 规范化入口也是运行覆盖的入口；不只测试 YAML loader，否则字符串 false 仍可能在覆盖层变真。
# 函数用途: 验证两个开关的共用归一行为。
def test_switch_normalizer_is_shared_with_runtime_overrides():
    normalized, warnings = normalize_agent_config(dict.fromkeys(_SWITCHES, "false"))
    assert {key: normalized[key] for key in _SWITCHES} == dict.fromkeys(_SWITCHES, False)
    assert not any(any(key in warning for key in _SWITCHES) for warning in warnings)


# LLM: 来源 kind/value 属 owner/persona 单一权威，不能以全局 schema、YAML 或参数登记变成另一份配置。
# 函数用途: 验证全局只暴露两个开关，旧式平铺来源键被过滤并告警。
def test_owner_source_kind_and_value_are_not_global_settings(monkeypatch):
    names = {item.name for item in fields(AgentConfig)}
    assert {name for name in names if name.startswith("agents_source_")} == set(_SWITCHES)
    assert {name for name in load_simple_yaml(_PACKAGED) if name.startswith("agents_source_")} == set(_SWITCHES)
    assert {name for name in parameter_registry() if name.startswith("agents_source_")} == set(_SWITCHES)
    raw = {"agents_source_kind": "url", "agents_source_value": "owner-only-source"}
    loaded = _load_raw(monkeypatch, raw)
    for key in raw:
        assert not hasattr(loaded, key)
        assert any(key in warning and "unknown config key" in warning for warning in loaded.config_warnings)


# LLM: 缺文件/读取失败不得伪造一次成功配置加载；异常传播不应尝试任何网络恢复。
# 函数用途: 验证正式 loader 对缺失及不可读部署配置的失败行为。
def test_missing_configuration_raises_instead_of_loading_defaults(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda _path: False)
    with pytest.raises(FileNotFoundError):
        load_config(_PACKAGED)


# LLM: IO 错误必须沿正式加载入口传播，不吞错并擅自开启来源。
# 函数用途: 验证不可读配置不会产生一个伪造的有效实例。
def test_unreadable_configuration_propagates_loading_failure(monkeypatch):
    # LLM: 只模拟 IO 权限错误，不执行来源加载。
    # 函数用途: 给正式 loader 提供明确的读取失败。
    def denied(_path):
        raise PermissionError("configuration is unreadable")

    monkeypatch.setattr(config_module, "load_simple_yaml", denied)
    with pytest.raises(PermissionError, match="unreadable"):
        load_config(_PACKAGED)


# LLM: 只覆盖配置加载这一观察点；这不代表来源网络入口已经受开关约束，父级须另跑真实入口。
# 函数用途: 验证默认配置加载本身不会发起新的网络请求。
def test_loading_default_configuration_does_not_make_network_requests(monkeypatch):
    # LLM: 任何到达网络原语的请求都使本配置测试失败。
    # 函数用途: 作为配置加载观察点的网络请求哨兵。
    def forbidden(*_args, **_kwargs):
        raise AssertionError("config loading must not make a network request")

    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    loaded = load_config(_PACKAGED)
    assert loaded.agents_source_enabled is True
    assert loaded.agents_source_network_enabled is False


# LLM: 展示目录必须来自原生成器，不能复制另一份手工默认或假定立即生效；安全标注不是请求许可。
# 函数用途: 验证两字段的生成目录默认、说明、生效时机及网络风险标记。


def test_source_switches_are_user_only_security_boundaries():
    from agent_py_agent.agent.settings.user_config_capability import USER_SETTINGS_BOUNDARY_KEYS
    registry = parameter_registry()
    for key in _SWITCHES:
        assert registry[key].safety == "boundary"
        assert registry[key].writable is False
        assert key in USER_SETTINGS_BOUNDARY_KEYS


# LLM: 直接调用实际模型配置工具；不进入可信用户写作用域，模型set/reset都不得借来源开关扩大能力。
# 函数用途: 验证user_config无法关闭规则来源或自行开启网络来源。
@pytest.mark.parametrize('key,value', [('agents_source_enabled', 'false'), ('agents_source_network_enabled', 'true')])
@pytest.mark.parametrize('action', ['set', 'reset'])
def test_model_user_config_cannot_change_source_boundaries(tmp_path, monkeypatch, key, value, action):
    from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
    user = tmp_path / 'desktop.yaml'
    user.write_text('agents_source_enabled: true\nagents_source_network_enabled: false\n', encoding='utf-8')
    monkeypatch.setenv('MY_AGENT_CONFIG', str(user))
    before = user.read_bytes()
    from types import SimpleNamespace
    host = SimpleNamespace(config=SimpleNamespace(config_path=str(user)),
        home_paths=SimpleNamespace(owner_provider='local', owner_kind='main', owner_id='main'))
    result = UserConfigTool(host).execute({'action': action, 'key': key, 'value': value})
    assert not result.ok
    assert '安全边界' in result.output
    assert user.read_bytes() == before


# LLM: 使用当前已认证HTTP→原持久控制→settings用户写作用域；不向模型传user_authorized，也不替换参数写服务。
# 函数用途: 验证真正用户可经现有设置入口修改来源开关，普通owner不能借该全局控制跨owner写。
@pytest.mark.parametrize('admin', [True, False])
def test_authenticated_settings_uses_original_user_control(tmp_path, monkeypatch, admin):
    from types import SimpleNamespace

    from agent_py_agent.agent.auth.models import Permission, Role
    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    user = tmp_path / 'desktop.yaml'
    user.write_text('agents_source_enabled: true\nagents_source_network_enabled: false\n', encoding='utf-8')
    loaded = load_config(user)
    base = SimpleNamespace(config=loaded, home_paths=home_paths(tmp_path / 'isolated-home'))
    replies = []
    handler = SimpleNamespace(_auth_middleware=object(), _send_json=lambda status, body: replies.append((status, body)))
    monkeypatch.setattr(http_handlers, '_request_identity', lambda _: ('fixture-user', Permission(Role.ADMIN if admin else Role.USER, 'fixture-user', ['chat'])))
    server = SimpleNamespace(agent=base, paths=gateway_paths_from_root(tmp_path / 'gateway'))
    owner = OwnerIdentity.local_main() if admin else OwnerIdentity.provider_user('feishu', 'other-user')
    for i, (key, value) in enumerate([('agents_source_enabled', 'false'), ('agents_source_network_enabled', 'true')]):
        before = user.read_bytes()
        text = f'/settings set {key} {value}'
        scope = GatewayControlScope('fixture-user', 'chat', 'settings-test', {'message_id': f'settings-{i}'}, resolved_owner=owner)
        http_handlers._handle_persistent_control_operation(handler, server,
            command=parse_conversation_control(text), command_text=text, scope=scope)
        status, result = replies[-1]
        assert status == 200
        assert result['ok'] is admin, result
        if not admin:
            assert user.read_bytes() == before
    if admin:
        actual = load_config(user)
        assert actual.agents_source_enabled is False
        assert actual.agents_source_network_enabled is True
