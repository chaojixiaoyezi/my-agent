"""Tests for settings/memory.py: memory config, routing rules, and injection modes.

给人看的解释：
测试记忆配置模块：记忆配置、路由规则、注入模式。
"""
import tempfile
from pathlib import Path

import pytest

from agent_py_agent.agent.settings.memory import (
    MemoryConfigWarning,
    MemorySettings,
    normalize_agent_memory_config,
    normalize_memory_settings,
)


class TestMemorySettingsDefaults:
    """测试 MemorySettings 默认值。"""

    def test_memory_archive_level_default(self):
        """验证归档级别默认值。"""
        settings = MemorySettings()
        assert settings.memory_archive_level == 3

    def test_memory_hook_enabled_default(self):
        """验证 hook 启用默认值。"""
        settings = MemorySettings()
        assert settings.memory_hook_enabled is True

    def test_memory_hook_archive_level_default(self):
        """验证 hook 归档级别默认值。"""
        settings = MemorySettings()
        assert settings.memory_hook_archive_level == 3

    def test_memory_hook_retention_days_default(self):
        """验证 hook 保留天数默认值。"""
        settings = MemorySettings()
        assert settings.memory_hook_retention_days == 7

    def test_memory_routing_defaults(self):
        """验证路由默认值。"""
        settings = MemorySettings()
        assert settings.memory_rule_routing_enabled is True
        assert settings.memory_rule_routing_mode == "soft"
        assert settings.memory_rule_auto_read_limit == 3

    def test_memory_resume_defaults(self):
        """验证恢复默认值。"""
        settings = MemorySettings()
        assert settings.memory_resume_auto_context_enabled is False
        assert settings.memory_resume_auto_context_mode == "trigger"
        assert settings.memory_resume_auto_context_limit == 5


class TestNormalizeMemorySettings:
    """测试 normalize_memory_settings 归一化。"""

    def test_normalize_valid_archive_level_0(self):
        """验证有效的归档级别 0。"""
        settings, warnings = normalize_memory_settings({"memory_archive_level": 0})
        assert settings.memory_archive_level == 0
        assert len(warnings) == 0

    def test_normalize_valid_archive_level_3(self):
        """验证有效的归档级别 3。"""
        settings, warnings = normalize_memory_settings({"memory_archive_level": 3})
        assert settings.memory_archive_level == 3
        assert len(warnings) == 0

    def test_normalize_invalid_archive_level_negative(self):
        """验证负数归档级别回退。"""
        settings, warnings = normalize_memory_settings({"memory_archive_level": -1})
        assert settings.memory_archive_level == 3  # 默认值
        assert len(warnings) == 1

    def test_normalize_invalid_archive_level_too_high(self):
        """验证过高的归档级别回退。"""
        settings, warnings = normalize_memory_settings({"memory_archive_level": 10})
        assert settings.memory_archive_level == 3  # 默认值
        assert len(warnings) == 1

    def test_normalize_valid_hook_enabled_true(self):
        """验证有效的 hook 启用 true。"""
        settings, warnings = normalize_memory_settings({"memory_hook_enabled": True})
        assert settings.memory_hook_enabled is True
        assert len(warnings) == 0

    def test_normalize_valid_hook_enabled_false(self):
        """验证有效的 hook 启用 false。"""
        settings, warnings = normalize_memory_settings({"memory_hook_enabled": False})
        assert settings.memory_hook_enabled is False
        assert len(warnings) == 0

    def test_normalize_invalid_hook_enabled_string(self):
        """验证无效的 hook 启用字符串回退。"""
        settings, warnings = normalize_memory_settings({"memory_hook_enabled": "maybe"})
        assert settings.memory_hook_enabled is True  # 默认值
        assert len(warnings) == 1

    def test_normalize_valid_routing_mode_soft(self):
        """验证有效的 soft 路由模式。"""
        settings, warnings = normalize_memory_settings({"memory_rule_routing_mode": "soft"})
        assert settings.memory_rule_routing_mode == "soft"
        assert len(warnings) == 0

    def test_normalize_valid_routing_mode_strict(self):
        """验证有效的 strict 路由模式。"""
        settings, warnings = normalize_memory_settings({"memory_rule_routing_mode": "strict"})
        assert settings.memory_rule_routing_mode == "strict"
        assert len(warnings) == 0

    def test_normalize_valid_routing_mode_off(self):
        """验证有效的 off 路由模式。"""
        settings, warnings = normalize_memory_settings({"memory_rule_routing_mode": "off"})
        assert settings.memory_rule_routing_mode == "off"
        assert len(warnings) == 0

    def test_normalize_invalid_routing_mode(self):
        """验证无效的路由模式回退。"""
        settings, warnings = normalize_memory_settings({"memory_rule_routing_mode": "invalid"})
        assert settings.memory_rule_routing_mode == "soft"  # 默认值
        assert len(warnings) == 1

    def test_normalize_valid_auto_read_limit(self):
        """验证有效的自动读取限制。"""
        settings, warnings = normalize_memory_settings({"memory_rule_auto_read_limit": 5})
        assert settings.memory_rule_auto_read_limit == 5
        assert len(warnings) == 0

    def test_normalize_invalid_auto_read_limit_negative(self):
        """验证负数自动读取限制回退。"""
        settings, warnings = normalize_memory_settings({"memory_rule_auto_read_limit": -1})
        assert settings.memory_rule_auto_read_limit == 3  # 默认值
        assert len(warnings) == 1

    def test_normalize_valid_retention_days(self):
        """验证有效的保留天数。"""
        settings, warnings = normalize_memory_settings({"memory_hook_retention_days": 14})
        assert settings.memory_hook_retention_days == 14
        assert len(warnings) == 0

    def test_normalize_zero_retention_days(self):
        """验证 0 保留天数（不保留）。"""
        settings, warnings = normalize_memory_settings({"memory_hook_retention_days": 0})
        assert settings.memory_hook_retention_days == 0
        assert len(warnings) == 0

    def test_normalize_valid_resume_mode_trigger(self):
        """验证有效的 trigger 恢复模式。"""
        settings, warnings = normalize_memory_settings({"memory_resume_auto_context_mode": "trigger"})
        assert settings.memory_resume_auto_context_mode == "trigger"
        assert len(warnings) == 0

    def test_normalize_valid_resume_mode_always(self):
        """验证有效的 always 恢复模式。"""
        settings, warnings = normalize_memory_settings({"memory_resume_auto_context_mode": "always"})
        assert settings.memory_resume_auto_context_mode == "always"
        assert len(warnings) == 0

    def test_normalize_invalid_resume_mode(self):
        """验证无效的恢复模式回退。"""
        settings, warnings = normalize_memory_settings({"memory_resume_auto_context_mode": "never"})
        assert settings.memory_resume_auto_context_mode == "trigger"  # 默认值
        assert len(warnings) == 1

    def test_normalize_valid_resume_limit(self):
        """验证有效的恢复限制。"""
        settings, warnings = normalize_memory_settings({"memory_resume_auto_context_limit": 10})
        assert settings.memory_resume_auto_context_limit == 10
        assert len(warnings) == 0

    def test_normalize_resume_limit_too_low(self):
        """验证过低的恢复限制回退。"""
        settings, warnings = normalize_memory_settings({"memory_resume_auto_context_limit": 0})
        assert settings.memory_resume_auto_context_limit == 5  # 默认值（最小1）
        assert len(warnings) == 1

    def test_normalize_resume_limit_too_high(self):
        """验证过高的恢复限制回退。"""
        settings, warnings = normalize_memory_settings({"memory_resume_auto_context_limit": 100})
        assert settings.memory_resume_auto_context_limit == 5  # 默认值（最大50）
        assert len(warnings) == 1


class TestNormalizeAgentMemoryConfig:
    """测试 normalize_agent_memory_config 配置归一化。

    注意：normalize_agent_memory_config 作用于 AgentConfig 等非冻结对象，
    而不是 MemorySettings（后者是 frozen=True）。
    这里用普通类模拟 AgentConfig 的 memory 属性结构。
    """

    def test_normalize_agent_memory_config_basic(self):
        """验证基本归一化。"""
        from dataclasses import dataclass

        @dataclass
        class MockConfig:
            memory_archive_level: int = 3
            memory_hook_enabled: bool = True

        config = MockConfig()
        warnings = normalize_agent_memory_config(config)
        assert isinstance(warnings, list)
        # 验证值被正确设置
        assert config.memory_archive_level == 3
        assert config.memory_hook_enabled is True

    def test_normalize_agent_memory_config_custom_values(self):
        """验证自定义值归一化。"""
        from dataclasses import dataclass

        @dataclass
        class MockConfig:
            memory_archive_level: int = 1
            memory_hook_enabled: bool = False
            memory_rule_routing_mode: str = "strict"

        config = MockConfig()
        warnings = normalize_agent_memory_config(config)
        # 自定义值保持不变
        assert config.memory_archive_level == 1
        assert config.memory_hook_enabled is False
        assert config.memory_rule_routing_mode == "strict"

    def test_normalize_agent_memory_config_stores_warnings(self):
        """验证警告被存储到 config。"""
        from dataclasses import dataclass

        @dataclass
        class MockConfig:
            memory_archive_level: int = 3

        config = MockConfig()
        normalize_agent_memory_config(config)
        # 检查 warnings 是否被设置
        assert hasattr(config, "memory_config_warnings")

    def test_normalize_agent_memory_config_invalid_value(self):
        """验证无效值产生警告。"""
        from dataclasses import dataclass

        @dataclass
        class MockConfig:
            memory_archive_level: int = 99  # 无效值

        config = MockConfig()
        warnings = normalize_agent_memory_config(config)
        # 无效值回退，生成警告
        assert config.memory_archive_level == 3  # 默认值
        assert len(warnings) > 0


class TestMemoryConfigWarning:
    """测试 MemoryConfigWarning 结构。"""

    def test_memory_config_warning_to_dict(self):
        """验证 warning 转换为字典。"""
        warning = MemoryConfigWarning(
            field_name="memory_archive_level",
            raw_value=-1,
            default_value=3,
            reason="expected integer 0-3",
        )
        d = warning.to_dict()
        assert d["field_name"] == "memory_archive_level"
        assert d["raw_value"] == -1
        assert d["default_value"] == 3
        assert d["reason"] == "expected integer 0-3"

    def test_memory_config_warning_fields(self):
        """验证 warning 字段。"""
        warning = MemoryConfigWarning(
            field_name="test_field",
            raw_value="bad",
            default_value="good",
            reason="test reason",
        )
        assert warning.field_name == "test_field"
        assert warning.raw_value == "bad"
        assert warning.default_value == "good"
        assert warning.reason == "test reason"


class TestMemorySettingsWithNone:
    """测试 None 输入处理。"""

    def test_normalize_memory_settings_none(self):
        """验证 None 输入使用全默认值。"""
        settings, warnings = normalize_memory_settings(None)
        assert settings.memory_archive_level == 3
        assert settings.memory_hook_enabled is True
        assert len(warnings) == 0
