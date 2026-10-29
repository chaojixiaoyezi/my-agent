# LLM: 压缩只读 canonical 50–95字段，模型窗口只经显式离线导入进入目录，不恢复旧运行回退。
# 模块用途: 验证记忆规范化、压缩告警与模型窗口投影。
from __future__ import annotations

"""memory 配置安全解析测试。"""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model.context_window import resolve_model_context_window_tokens
from agent_py_agent.agent.agent_core.runtime.context_compactor import (
    compact_recovery_target_percent,
    compact_recovery_target_tokens,
    compact_trigger_percent,
    compact_trigger_tokens,
    runtime_compact_policy,
)
from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.memory import MemorySettings, normalize_memory_settings


def test_memory_settings_defaults_have_no_warnings():
    settings, warnings = normalize_memory_settings({})

    assert settings == MemorySettings()
    assert warnings == []


# LLM: 合法边界不制造告警，canonical压缩字段必须保持原显式值。
# 函数用途: 验证记忆配置合法类型及边界。
def test_memory_settings_accepts_boundary_values():
    settings, warnings = normalize_memory_settings(
        {
            "memory_archive_level": 0,
            "memory_hook_enabled": "false",
            "memory_rule_routing_mode": "STRICT",
            "memory_rule_auto_read_limit": "0",
            "memory_resume_auto_context_mode": "ALWAYS",
            "memory_compact_trigger_percent": "70",
            "memory_compact_recovery_target_percent": "55",
        }
    )

    assert warnings == []
    assert settings.memory_archive_level == 0
    assert settings.memory_hook_enabled is False
    assert settings.memory_rule_routing_mode == "strict"
    assert settings.memory_rule_auto_read_limit == 0
    assert settings.memory_resume_auto_context_mode == "always"
    assert settings.memory_compact_trigger_percent == 70
    assert settings.memory_compact_recovery_target_percent == 55


# LLM: 非法输入回默认且逐字段留下告警，不能丢掉压缩键的诊断。
# 函数用途: 验证无效记忆配置有准确告警。
def test_memory_settings_invalid_values_fall_back_with_warnings():
    settings, warnings = normalize_memory_settings(
        {
            "memory_archive_level": "abcd",
            "memory_hook_enabled": "false; rm -rf /",
            "memory_rule_routing_mode": "strict; rm -rf /",
            "memory_rule_auto_read_limit": -5,
            "memory_resume_auto_context_mode": "always; rm -rf /",
            "memory_compact_trigger_percent": "abc",
            "memory_compact_recovery_target_percent": "abc",
        }
    )

    assert settings == MemorySettings()
    assert {warning.field_name for warning in warnings} == {
        "memory_archive_level",
        "memory_hook_enabled",
        "memory_rule_routing_mode",
        "memory_rule_auto_read_limit",
        "memory_resume_auto_context_mode",
        "memory_compact_trigger_percent",
        "memory_compact_recovery_target_percent",
    }
    assert all(warning.default_value is not None for warning in warnings)


# LLM: 从正式加载器验证落值与告警顺序，不能只测底层 helper。
# 函数用途: 验证配置文件的记忆规范化回执。
def test_load_config_normalizes_memory_values_and_keeps_warning_receipts(tmp_path):
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text("\n".join(_memory_config_yaml_lines()), encoding="utf-8")

    config = load_config(config_path)

    assert config.memory_archive_level == 3
    assert config.memory_hook_enabled is True
    assert config.memory_rule_routing_mode == "off"
    assert config.memory_rule_auto_read_limit == 3
    assert config.memory_resume_auto_context_mode == "trigger"
    assert config.memory_compact_trigger_percent == 50
    assert config.memory_compact_recovery_target_percent == 80
    assert [item["field_name"] for item in config.memory_config_warnings] == [
        "memory_archive_level",
        "memory_hook_enabled",
        "memory_rule_auto_read_limit",
        "memory_compact_trigger_percent",
        "memory_compact_recovery_target_percent",
    ]


# LLM: 非法样本只写测试临时目录，不修改真实配置。
# 函数用途: 提供含 canonical 压缩比例的测试样本。
def _memory_config_yaml_lines() -> list[str]:
    return [
        "memory_archive_level: 99",
        "memory_hook_enabled: maybe",
        "memory_rule_routing_mode: off",
        "memory_rule_auto_read_limit: abcd",
        "memory_resume_auto_context_mode: trigger",
        "memory_compact_trigger_percent: 40",
        "memory_compact_recovery_target_percent: 99",
    ]


# LLM: canonical零不能关闭压缩，越界须夹取告警，不复活旧0到100映射。
# 函数用途: 验证新压缩比例的上下界和告警。
def test_memory_compact_trigger_percent_zero_and_large_values_are_clamped_with_warnings():
    zero, zero_warnings = normalize_memory_settings({"memory_compact_trigger_percent": 0})
    large, large_warnings = normalize_memory_settings({"memory_compact_trigger_percent": 120})

    assert zero.memory_compact_trigger_percent == 50
    assert [item.field_name for item in zero_warnings] == ["memory_compact_trigger_percent"]
    assert large.memory_compact_trigger_percent == 95
    assert [item.field_name for item in large_warnings] == ["memory_compact_trigger_percent"]


# LLM: 新默认唯一为80，MemorySettings不持有退役别名。
# 函数用途: 验证压缩默认值和旧字段删除。
def test_memory_compact_trigger_percent_default_is_80():
    settings, warnings = normalize_memory_settings({})

    assert settings.memory_compact_trigger_percent == 80
    assert not hasattr(settings, "memory_compact_auto_trigger_percent")
    assert settings.memory_compact_recovery_target_percent == 60
    assert warnings == []


def test_runtime_compact_policy_percent_parser_matches_config_semantics():
    assert compact_trigger_percent(None) == 90
    assert compact_trigger_percent("abc") == 90
    assert compact_trigger_percent(0) == 100
    assert compact_trigger_percent(40) == 50
    assert compact_trigger_percent(120) == 100
    assert compact_trigger_tokens(200_000, 70) == 140_000
    assert compact_recovery_target_percent(None) == 60
    assert compact_recovery_target_percent(10) == 25
    assert compact_recovery_target_percent(90) == 80
    assert compact_recovery_target_tokens(128_000, 115_200, 11_520, 60) == 76_800
    assert compact_recovery_target_tokens(128_000, 64_000, 11_520, 60) == 52_480


# LLM: 正式转录上下文仍可持久压缩，save=False不破坏原权威标志语义。
# 函数用途: 验证 canonical 配置下的运行压缩授权。
def test_runtime_compact_policy_allows_authoritative_no_save_turn() -> None:
    agent = SimpleNamespace(
        config=SimpleNamespace(
            memory_compact_trigger_percent=80,
            memory_compact_recovery_target_percent=60,
        ),
        backend=SimpleNamespace(context_window_tokens=128_000),
    )

    auxiliary = runtime_compact_policy(agent, save=False)
    authoritative = runtime_compact_policy(
        agent,
        save=False,
        task_attributes={CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True},
    )

    assert auxiliary.allow_persistent_apply is False
    assert authoritative.allow_persistent_apply is True
    assert authoritative.recovery_target_percent == 60
    assert authoritative.recovery_target_tokens == 76_800


# LLM: 旧窗口先导入临时管理员目录，再由原选择解析到后端；不发送模型请求。
# 函数用途: 验证目录窗口贯通后端和上下文解析。
def test_model_context_window_config_reaches_http_backend(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.settings.config_io import load_simple_yaml
    from agent_py_agent.agent.settings.model_credentials import import_legacy_owner_configuration
    from agent_py_agent.agent.settings.model_profiles import selected_model_config_for_resources
    from agent_py_agent.agent.user_space.home_layout import home_paths

    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "model_backend: anthropic_compatible",
                "api_base: https://example.invalid/anthropic",
                "api_key: test-key",
                "model_name: test-model",
                "model_context_window_tokens: 234567",
            ]
        ),
        encoding="utf-8",
    )

    home = replace(home_paths(tmp_path / "home"), owner_provider="local", owner_kind="main", owner_id="main")
    import_legacy_owner_configuration(home, agent_values=load_simple_yaml(config_path), capability_values={},
                                      environ={}, expected_generation=None)
    config = selected_model_config_for_resources(load_config(config_path), home)
    backend = get_backend(config.model_backend, config)

    assert backend.context_window_tokens == 234567
    assert resolve_model_context_window_tokens(SimpleNamespace(backend=backend)) == 234567
