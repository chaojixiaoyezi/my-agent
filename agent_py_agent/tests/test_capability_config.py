"""Tests for capability/config.py: capability config loading, defaults, and permission control.

给人看的解释：
测试能力配置模块：能力配置加载、默认值、权限控制。
"""
import tempfile
from dataclasses import fields
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.config import (
    CapabilityConfig,
    load_capability_config,
)
from agent_py_agent.agent.settings.config import load_simple_yaml

SHIPPED_TEMPLATE = Path(__file__).parents[1] / "config" / "capability_config.yaml"


class TestCapabilityConfigDefaults:
    """测试 CapabilityConfig 默认值。"""

    def test_capability_config_default_enable(self):
        """验证包推荐默认开启、一次选包默认关闭。"""
        config = CapabilityConfig()
        assert config.enable_capability_package_recommendations is True
        assert config.enable_capability_package_selection is False

    def test_capability_config_default_timeouts(self):
        """验证子代理默认不设置系统级超时墙。"""
        config = CapabilityConfig()
        assert config.subagent_run_timeout == 0
        assert config.subagent_heartbeat_timeout == 0

    def test_capability_config_default_limits(self):
        """验证限制默认值。"""
        config = CapabilityConfig()
        assert config.capability_candidate_limit == 5
        assert config.subagent_no_progress_attempt_limit == 4
        assert config.subagent_stream_activity_projection_enabled is True
        assert config.subagent_stream_activity_interval_seconds == 15

    def test_capability_config_zero_means_unlimited(self):
        """验证 0 表示不限制。"""
        config = CapabilityConfig()
        assert config.capability_grant_max_skills == 0
        assert config.capability_grant_max_tools == 0


class TestLoadCapabilityConfig:
    """测试 load_capability_config 配置加载。"""

    def test_load_capability_config_file_not_found(self):
        """验证文件不存在时抛出异常。"""
        with pytest.raises(FileNotFoundError) as exc_info:
            load_capability_config("/nonexistent/path/capability.yaml")
        assert "能力路由配置文件不存在" in str(exc_info.value)

    def test_load_capability_config_valid_values(self):
        """验证加载有效配置。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("enable_capability_package_selection: true\n")
            f.write("capability_package_selection_max_input_tokens: 800\n")
            f.flush()
            path = Path(f.name)

        try:
            config = load_capability_config(path)
            assert config.enable_capability_package_selection is True
            assert config.capability_package_selection_max_input_tokens == 800
        finally:
            path.unlink()

    def test_load_capability_config_partial_values(self):
        """验证部分配置加载。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("enable_capability_package_selection: true\n")
            f.flush()
            path = Path(f.name)

        try:
            config = load_capability_config(path)
            assert config.enable_capability_package_selection is True
            # 其他字段使用默认值
            assert config.capability_package_selection_max_input_tokens == 3000
        finally:
            path.unlink()

    def test_load_capability_config_unknown_fields_rejected(self):
        """验证未知字段会直接报错。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("enable_capability_package_selection: true\n")
            f.write("unknown_field: value\n")
            f.flush()
            path = Path(f.name)

        try:
            # 2026-09-27 起与主配置一致：未知键只告警并忽略，不再拒绝加载（参数减量约定）。
            config = load_capability_config(path)
            assert any("unknown_field" in w for w in config.config_warnings)
            assert config.enable_capability_package_selection is True
        finally:
            path.unlink()

    def test_load_capability_config_empty_file(self):
        """验证空文件使用所有默认值。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("")
            f.flush()
            path = Path(f.name)

        try:
            config = load_capability_config(path)
            # 所有字段应该使用默认值
            assert config.enable_capability_package_selection is False
            assert config.capability_package_selection_max_input_tokens == 3000
        finally:
            path.unlink()

    def test_load_capability_config_with_integers(self):
        """验证整数类型配置项。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("subagent_failure_split_max_depth: 6\n")
            f.write("subagent_heartbeat_timeout: 300\n")
            f.flush()
            path = Path(f.name)

        try:
            config = load_capability_config(path)
            assert config.subagent_failure_split_max_depth == 6
            assert config.subagent_heartbeat_timeout == 300
        finally:
            path.unlink()

    def test_load_capability_config_with_booleans(self):
        """验证布尔类型配置项。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("enable_capability_package_selection: true\n")
            f.write("capability_grant_expires_after_task: false\n")
            f.write("subagent_stream_activity_projection_enabled: false\n")
            f.flush()
            path = Path(f.name)

        try:
            config = load_capability_config(path)
            assert config.enable_capability_package_selection is True
            assert config.capability_grant_expires_after_task is False
            assert config.subagent_stream_activity_projection_enabled is False
        finally:
            path.unlink()


class TestCapabilityConfigValidation:
    """测试 CapabilityConfig 字段验证。"""

    def test_capability_config_token_limits(self):
        """验证 token 限制字段。"""
        config = CapabilityConfig()
        assert config.capability_package_selection_max_input_tokens == 3000
        assert config.capability_bundle_max_tokens == 3000

    def test_capability_config_timeout_hierarchy(self):
        """验证默认不靠超时层级限制子代理。"""
        config = CapabilityConfig()
        assert config.subagent_heartbeat_timeout == 0
        assert config.subagent_run_timeout == 0

    def test_capability_config_grant_expiration(self):
        """验证授权过期设置。"""
        config = CapabilityConfig()
        assert config.capability_grant_expires_after_task is True


class TestCapabilityConfigEdgeCases:
    """测试 CapabilityConfig 边界情况。"""

    def test_capability_config_all_zero_limits(self):
        """验证全 0 限制值。"""
        config = CapabilityConfig(
            capability_grant_max_skills=0,
            capability_grant_max_tools=0,
            capability_candidate_limit=0,
            subagent_failure_split_max_depth=0,
        )
        # 全 0 表示不限制
        assert config.capability_grant_max_skills == 0
        assert config.capability_candidate_limit == 0

    def test_capability_config_custom_limits(self):
        """验证自定义限制值。"""
        config = CapabilityConfig(
            capability_candidate_limit=10,
            capability_bundle_max_tokens=5000,
            subagent_no_progress_attempt_limit=5,
        )
        assert config.capability_candidate_limit == 10
        assert config.capability_bundle_max_tokens == 5000
        assert config.subagent_no_progress_attempt_limit == 5


# 函数用途: 列出 CapabilityConfig 里真正的配置键；config_warnings 是加载诊断，不是配置项。
def _config_field_names() -> set[str]:
    return {item.name for item in fields(CapabilityConfig)} - {"config_warnings"}


class TestShippedCapabilityTemplate:
    """随包 capability_config.yaml 是默认模板。

    Gateway 找不到 owner 下的配置文件时按 dataclass 默认值运行，daemon/subagents 等 CLI 子命令默认直接读模板，
    两边必须一致，否则同一台机器上不同入口的行为会互相打架。
    """

    def test_every_template_key_is_a_field_with_the_dataclass_default(self):
        """模板里的每个键都要被 CapabilityConfig 认识、加载无告警，且值等于 dataclass 默认值。"""
        raw = load_simple_yaml(SHIPPED_TEMPLATE)
        loaded = load_capability_config(SHIPPED_TEMPLATE)
        defaults = CapabilityConfig()
        assert loaded.config_warnings == []
        assert set(raw) <= _config_field_names()
        mismatched = {key: (getattr(loaded, key), getattr(defaults, key))
                      for key in raw if getattr(loaded, key) != getattr(defaults, key)}
        assert mismatched == {}

    def test_every_config_field_is_in_the_template(self):
        """CapabilityConfig 的每个字段都必须写进模板（带中文注释），不能只藏在 dataclass 里；不用的字段直接删。"""
        assert _config_field_names() - set(load_simple_yaml(SHIPPED_TEMPLATE)) == set()
