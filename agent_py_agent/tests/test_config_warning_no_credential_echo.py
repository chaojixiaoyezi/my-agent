"""配置告警不得回显凭据类参数的原值（2026-09-27，dev 审 a95746edc 时发现）。

背景：`/settings` 总览现在会显示配置告警。而 `settings/services/_normalize.py` 里有几处告警把用户写的原始值
原样带进文字（`got {value!r}`）。用户若把凭据类参数写成错误类型，值就会经 `/settings` 显示出来，飞书上也能看到。
修法是**在源头**统一经一个 helper 输出：凭据键不回显值（写"已隐藏"），其他键照旧但截短。

本测试锁定：凭据键的告警不含原值且写明已隐藏；普通键照旧显示；超长值被截短并标注。
"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.settings.normalize import normalize_agent_config
from agent_py_agent.agent.settings.user_config_capability import is_credential_key

_SECRET = "<redacted>"


def _warnings_for(raw: dict) -> list[str]:
    _normalized, warnings = normalize_agent_config(raw)
    return list(warnings)


def _for_key(raw: dict, key: str) -> list[str]:
    return [w for w in _warnings_for(raw) if key in w]


# ---- 会真正走到回显分支的字段（都是非凭据类，用来验证"普通键照旧"） ----
_ECHO_FIELDS = ("my_agent_home", "workspace_task_path_template")


def test_echo_fields_are_not_credential_keys() -> None:
    """前提校验：用来测"普通键"的字段确实不是凭据类。"""
    assert not any(is_credential_key(field) for field in _ECHO_FIELDS)


@pytest.mark.parametrize("field", _ECHO_FIELDS)
def test_non_credential_key_still_echoes_its_value(field: str) -> None:
    """普通键照旧回显值，别把这条修得看不见问题。"""
    warnings = _for_key({field: {"nested": _SECRET}}, field)

    assert warnings, f"{field} 应当产生告警"
    assert _SECRET in warnings[0]


@pytest.mark.parametrize("field", _ECHO_FIELDS)
def test_long_value_is_truncated(field: str) -> None:
    """普通键的值太长要截短，避免把整段配置倒进聊天。"""
    warnings = _for_key({field: {"nested": "x" * 500}}, field)

    assert warnings
    assert len(warnings[0]) < 300, warnings[0]


def test_credential_key_never_echoes_the_value() -> None:
    """凭据键的错误类型值不能出现在告警里；helper 直接测，不依赖某个字段恰好走哪条分支。"""
    from agent_py_agent.agent.settings.services._normalize import describe_raw_value

    text = describe_raw_value("api_key", {"nested": _SECRET})

    assert _SECRET not in text
    assert "已隐藏" in text


@pytest.mark.parametrize(
    "key", ["api_key", "gateway_auth_token", "feishu_app_secret", "qq_app_secret", "feishu_verification_token"]
)
def test_helper_hides_every_credential_key(key: str) -> None:
    from agent_py_agent.agent.settings.services._normalize import describe_raw_value

    text = describe_raw_value(key, [_SECRET, "other"])

    assert _SECRET not in text
    assert "已隐藏" in text


def test_helper_truncates_long_non_credential_values() -> None:
    from agent_py_agent.agent.settings.services._normalize import describe_raw_value

    text = describe_raw_value("my_agent_home", "y" * 500)

    assert len(text) < 200
    assert "截断" in text


def test_helper_keeps_short_non_credential_values_readable() -> None:
    from agent_py_agent.agent.settings.services._normalize import describe_raw_value

    text = describe_raw_value("my_agent_home", "short")

    assert "short" in text
    assert "截断" not in text


# ===== 端到端：真实走到 /settings 总览，确认凭据原值不出现在回执里 =====

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import settings_control_service as module

_ADMIN_HOME = type("H", (), {"owner_provider": "local", "owner_kind": "main", "owner_id": "main"})()


def _settings_view(monkeypatch, tmp_path, raw_yaml: str) -> str:
    """用真实 load_config 加载一份含错误类型的用户配置，再走 /settings 总览。"""
    from agent_py_agent.agent.settings.config import load_config
    from agent_py_agent.agent.settings.user_config_capability import user_config_path

    base = tmp_path / "agent.yaml"
    base.write_text("", encoding="utf-8")
    config = load_config(base)
    path = user_config_path(config)
    path.write_text(raw_yaml, encoding="utf-8")

    loaded = load_config(base)
    monkeypatch.setattr(module, "_scoped_home", lambda _agent, _scope: _ADMIN_HOME)
    agent = type("A", (), {"config": loaded})()
    command = parse_conversation_control("/settings", reject_unknown_slash=True)
    result = module.execute_settings_control(agent, command, None)
    assert result.ok, result.message
    return result.message


def test_settings_overview_never_shows_a_credential_value(monkeypatch, tmp_path) -> None:
    """凭据键写成错误类型时，总览里不能出现原值。"""
    secret = "sk-live-DO-NOT-LEAK-abcdef1234567890"
    view = _settings_view(
        monkeypatch,
        tmp_path,
        f"api_key: {{nested: {secret}}}\ngateway_auth_token: [{secret}]\n",
    )

    assert secret not in view


def test_settings_overview_shows_a_credential_warning_as_hidden(monkeypatch, tmp_path) -> None:
    """凭据键出错时要留下痕迹（写"已隐藏"），不能让用户以为配置没错。

    输入用带引号的列表：项目自带的 YAML 读取只把 `["..."]` 解析成列表，`{nested: ...}` 和不带引号的 `[...]`
    都会整行读成字符串，本身就是合法字符串值。
    """
    secret = "sk-live-DO-NOT-LEAK-abcdef1234567890"
    view = _settings_view(monkeypatch, tmp_path, f'api_key: ["{secret}"]\n')

    assert secret not in view
    assert "api_key: expected a string, got 已隐藏（凭据不显示原值）; using default" in view


def test_settings_overview_still_shows_ordinary_warnings(monkeypatch, tmp_path) -> None:
    """普通键的告警照旧显示原值，别把这条修得什么都看不见。"""
    view = _settings_view(monkeypatch, tmp_path, "runner_timeout_by_role: not-a-dict\n")

    assert "配置告警" in view
    assert "runner_timeout_by_role" in view
    assert "not-a-dict" in view


# ===== 凭据字符串字段的类型校验（2026-09-28，T3 真实 TUI 验收发现：凭据键写错类型时没有任何告警） =====

from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.services._normalize import credential_string_fields

_CREDENTIAL_FIELDS = credential_string_fields(AgentConfig)


def _warnings_about(key: str, warnings: list[str]) -> list[str]:
    return [warning for warning in warnings if warning.startswith(f"{key}:")]


def test_credential_string_fields_come_from_the_config_declaration() -> None:
    """名单从 AgentConfig 声明推出；锁住已知凭据字段都在里面，防止推导失效后变成空名单。"""
    assert {
        "api_key", "gateway_auth_token", "feishu_app_secret",
        "feishu_verification_token", "feishu_encrypt_key", "qq_app_secret",
    } <= set(_CREDENTIAL_FIELDS)
    assert all(is_credential_key(key) for key in _CREDENTIAL_FIELDS)


@pytest.mark.parametrize("key", _CREDENTIAL_FIELDS)
@pytest.mark.parametrize("raw", [[_SECRET], {"nested": _SECRET}, True, 1.5], ids=["list", "dict", "bool", "float"])
def test_wrong_type_credential_warns_without_echo_and_falls_back(key: str, raw: object) -> None:
    """类型不符：告警一条、写“已隐藏”、不回显原值，运行值回落到默认值（不再原样进入或静默变空）。"""
    normalized, warnings = normalize_agent_config({key: raw})

    mine = _warnings_about(key, warnings)
    assert len(mine) == 1, warnings
    assert mine[0] == f"{key}: expected a string, got 已隐藏（凭据不显示原值）; using default"
    assert _SECRET not in "\n".join(warnings)
    assert normalized[key] == getattr(AgentConfig(), key)


@pytest.mark.parametrize("key", _CREDENTIAL_FIELDS)
def test_numeric_credential_keeps_the_legacy_string_restore(key: str) -> None:
    """纯数字没加引号被读成整数时，沿用原来的“按字符串还原”，不算类型错误。"""
    normalized, warnings = normalize_agent_config({key: 123456})

    assert normalized[key] == "123456"
    assert not _warnings_about(key, warnings)


@pytest.mark.parametrize("key", _CREDENTIAL_FIELDS)
@pytest.mark.parametrize("raw", [None, []], ids=["none", "empty-list"])
def test_blank_credential_means_default_without_warning(key: str, raw: object) -> None:
    """留空（YAML 里 `key:` 读成 []）按“没填”取默认值，不告警。"""
    normalized, warnings = normalize_agent_config({key: raw})

    assert normalized[key] == getattr(AgentConfig(), key)
    assert not _warnings_about(key, warnings)


def test_string_credential_is_kept_verbatim() -> None:
    normalized, warnings = normalize_agent_config({"feishu_verification_token": "sk-plain-value"})

    assert normalized["feishu_verification_token"] == "sk-plain-value"
    assert not _warnings_about("feishu_verification_token", warnings)


def test_loaded_config_falls_back_and_settings_reports_each_wrong_type(monkeypatch, tmp_path) -> None:
    """端到端：真实 load_config 后运行值回落默认值；/settings 总览按条数报告，原值不出现。"""
    secret = "sk-live-DO-NOT-LEAK-abcdef1234567890"
    raw_yaml = f'feishu_verification_token: ["{secret}"]\nfeishu_app_secret: ["{secret}"]\n'
    config_path = tmp_path / "loaded.yaml"
    config_path.write_text(raw_yaml, encoding="utf-8")
    loaded = load_config(config_path)
    assert loaded.feishu_verification_token == "" and loaded.feishu_app_secret == ""

    view = _settings_view(monkeypatch, tmp_path, raw_yaml)
    assert secret not in view
    assert "配置告警 2 条" in view
    assert "feishu_verification_token: expected a string, got 已隐藏" in view
    assert "feishu_app_secret: expected a string, got 已隐藏" in view
