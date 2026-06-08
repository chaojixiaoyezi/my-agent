from __future__ import annotations

"""memory 配置安全解析测试。"""

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model.context_window import resolve_model_context_window_tokens
from agent_py_agent.agent.agent_core.runtime.context_compactor import (
    compact_trigger_percent,
    compact_trigger_tokens,
)
from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.memory import MemorySettings, normalize_memory_settings


def test_memory_settings_defaults_have_no_warnings():
    settings, warnings = normalize_memory_settings({})

    assert settings == MemorySettings()
    assert warnings == []


def test_memory_settings_accepts_boundary_values():
    settings, warnings = normalize_memory_settings(
        {
            "memory_archive_level": 0,
            "memory_hook_enabled": "false",
            "memory_hook_archive_level": "3",
            "memory_hook_retention_days": 0,
            "memory_rule_routing_enabled": "off",
            "memory_rule_routing_mode": "STRICT",
            "memory_rule_auto_read_limit": "0",
            "memory_rule_receipt_enabled": 1,
            "memory_resume_auto_context_enabled": "true",
            "memory_resume_auto_context_mode": "ALWAYS",
            "memory_resume_auto_context_limit": "1",
            "memory_compact_auto_trigger_percent": "70",
        }
    )

    assert warnings == []
    assert settings.memory_archive_level == 0
    assert settings.memory_hook_enabled is False
    assert settings.memory_hook_archive_level == 3
    assert settings.memory_hook_retention_days == 0
    assert settings.memory_rule_routing_enabled is False
    assert settings.memory_rule_routing_mode == "strict"
    assert settings.memory_rule_auto_read_limit == 0
    assert settings.memory_rule_receipt_enabled is True
    assert settings.memory_resume_auto_context_enabled is True
    assert settings.memory_resume_auto_context_mode == "always"
    assert settings.memory_resume_auto_context_limit == 1
    assert settings.memory_compact_auto_trigger_percent == 70


def test_memory_settings_invalid_values_fall_back_with_warnings():
    settings, warnings = normalize_memory_settings(
        {
            "memory_archive_level": "abcd",
            "memory_hook_enabled": "false; rm -rf /",
            "memory_hook_archive_level": 4,
            "memory_hook_retention_days": -1,
            "memory_rule_routing_enabled": "乱码<script>",
            "memory_rule_routing_mode": "strict; rm -rf /",
            "memory_rule_auto_read_limit": -5,
            "memory_rule_receipt_enabled": "maybe",
            "memory_resume_auto_context_enabled": "maybe",
            "memory_resume_auto_context_mode": "always; rm -rf /",
            "memory_resume_auto_context_limit": 999,
            "memory_compact_auto_trigger_percent": "abc",
        }
    )

    assert settings == MemorySettings()
    assert {warning.field_name for warning in warnings} == {
        "memory_archive_level",
        "memory_hook_enabled",
        "memory_hook_archive_level",
        "memory_hook_retention_days",
        "memory_rule_routing_enabled",
        "memory_rule_routing_mode",
        "memory_rule_auto_read_limit",
        "memory_rule_receipt_enabled",
        "memory_resume_auto_context_enabled",
        "memory_resume_auto_context_mode",
        "memory_resume_auto_context_limit",
        "memory_compact_auto_trigger_percent",
    }
    assert all(warning.default_value is not None for warning in warnings)


def test_load_config_normalizes_memory_values_and_keeps_warning_receipts(tmp_path):
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text("\n".join(_memory_config_yaml_lines()), encoding="utf-8")

    config = load_config(config_path)

    assert config.memory_archive_level == 3
    assert config.memory_hook_enabled is True
    assert config.memory_hook_archive_level == 3
    assert config.memory_hook_retention_days == 14
    assert config.memory_rule_routing_enabled is True
    assert config.memory_rule_routing_mode == "off"
    assert config.memory_rule_auto_read_limit == 3
    assert config.memory_rule_receipt_enabled is False
    assert config.memory_resume_auto_context_enabled is True
    assert config.memory_resume_auto_context_mode == "trigger"
    assert config.memory_resume_auto_context_limit == 5
    assert config.memory_compact_auto_trigger_percent == 50
    assert [item["field_name"] for item in config.memory_config_warnings] == [
        "memory_archive_level",
        "memory_hook_enabled",
        "memory_hook_archive_level",
        "memory_rule_auto_read_limit",
        "memory_resume_auto_context_limit",
        "memory_compact_auto_trigger_percent",
    ]


def _memory_config_yaml_lines() -> list[str]:
    return [
        "memory_archive_level: 99",
        "memory_hook_enabled: maybe",
        "memory_hook_archive_level: -1",
        "memory_hook_retention_days: 14",
        "memory_rule_routing_enabled: true",
        "memory_rule_routing_mode: off",
        "memory_rule_auto_read_limit: abcd",
        "memory_rule_receipt_enabled: false",
        "memory_resume_auto_context_enabled: yes",
        "memory_resume_auto_context_mode: trigger",
        "memory_resume_auto_context_limit: 0",
        "memory_compact_auto_trigger_percent: 40",
    ]


def test_memory_compact_trigger_percent_zero_and_large_values_fall_back_to_100():
    zero, zero_warnings = normalize_memory_settings({"memory_compact_auto_trigger_percent": 0})
    large, large_warnings = normalize_memory_settings({"memory_compact_auto_trigger_percent": 120})

    assert zero.memory_compact_auto_trigger_percent == 100
    assert zero_warnings == []
    assert large.memory_compact_auto_trigger_percent == 100
    assert [item.field_name for item in large_warnings] == ["memory_compact_auto_trigger_percent"]


def test_memory_compact_trigger_percent_default_is_70():
    settings, warnings = normalize_memory_settings({})

    assert settings.memory_compact_auto_trigger_percent == 70
    assert warnings == []


def test_runtime_compact_policy_percent_parser_matches_config_semantics():
    assert compact_trigger_percent(None) == 70
    assert compact_trigger_percent("abc") == 70
    assert compact_trigger_percent(0) == 100
    assert compact_trigger_percent(40) == 50
    assert compact_trigger_percent(120) == 100
    assert compact_trigger_tokens(200_000, 70) == 140_000


def test_model_context_window_config_reaches_http_backend(tmp_path):
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

    config = load_config(config_path)
    backend = get_backend(config.model_backend, config)

    assert backend.context_window_tokens == 234567
    assert resolve_model_context_window_tokens(SimpleNamespace(backend=backend)) == 234567
