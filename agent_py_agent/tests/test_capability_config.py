"""Tests for capability/config.py: capability config loading, defaults, and permission control.

给人看的解释：
测试能力配置模块：能力配置加载、默认值、权限控制。
"""
import tempfile
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.config import (
    CapabilityConfig,
    load_capability_config,
)


class TestCapabilityConfigDefaults:
    """测试 CapabilityConfig 默认值。"""

    def test_capability_config_default_enable(self):
        """验证能力路由默认关闭。"""
        config = CapabilityConfig()
        assert config.enable_capability_routing is False

    def test_capability_config_default_timeouts(self):
        """验证子代理默认不设置系统级超时墙。"""
        config = CapabilityConfig()
        assert config.capability_request_max_tokens == 600
        assert config.subagent_run_timeout == 0
        assert config.subagent_heartbeat_timeout == 0

    def test_capability_config_default_limits(self):
        """验证限制默认值。"""
        config = CapabilityConfig()
        assert config.capability_candidate_limit == 5
        assert config.capability_alternative_max_attempts == 3
        assert config.subagent_no_progress_attempt_limit == 4
        assert config.subagent_stream_activity_projection_enabled is True
        assert config.subagent_stream_activity_interval_seconds == 15

    def test_capability_config_zero_means_unlimited(self):
        """验证 0 表示不限制。"""
        config = CapabilityConfig()
        assert config.capability_request_max_tried_items == 0
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
            f.write("enable_capability_routing: true\n")
            f.write("capability_request_max_tokens: 800\n")
            f.flush()
            path = Path(f.name)

        try:
            config = load_capability_config(path)
            assert config.enable_capability_routing is True
            assert config.capability_request_max_tokens == 800
        finally:
            path.unlink()

    def test_load_capability_config_partial_values(self):
        """验证部分配置加载。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("enable_capability_routing: true\n")
            f.flush()
            path = Path(f.name)

        try:
            config = load_capability_config(path)
            assert config.enable_capability_routing is True
            # 其他字段使用默认值
            assert config.capability_request_max_tokens == 600
        finally:
            path.unlink()

    def test_load_capability_config_unknown_fields_rejected(self):
        """验证未知字段会直接报错。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("enable_capability_routing: true\n")
            f.write("unknown_field: value\n")
            f.flush()
            path = Path(f.name)

        try:
            with pytest.raises(ValueError, match="未知字段"):
                load_capability_config(path)
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
            assert config.enable_capability_routing is False
            assert config.capability_request_max_tokens == 600
        finally:
            path.unlink()

    def test_load_capability_config_with_integers(self):
        """验证整数类型配置项。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("capability_escalation_max_hops: 6\n")
            f.write("subagent_due_check_interval: 300\n")
            f.flush()
            path = Path(f.name)

        try:
            config = load_capability_config(path)
            assert config.capability_escalation_max_hops == 6
            assert config.subagent_due_check_interval == 300
        finally:
            path.unlink()

    def test_load_capability_config_with_booleans(self):
        """验证布尔类型配置项。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("enable_capability_routing: true\n")
            f.write("capability_grant_expires_after_task: false\n")
            f.write("subagent_stream_activity_projection_enabled: false\n")
            f.flush()
            path = Path(f.name)

        try:
            config = load_capability_config(path)
            assert config.enable_capability_routing is True
            assert config.capability_grant_expires_after_task is False
            assert config.subagent_stream_activity_projection_enabled is False
        finally:
            path.unlink()


class TestCapabilityConfigValidation:
    """测试 CapabilityConfig 字段验证。"""

    def test_capability_config_token_limits(self):
        """验证 token 限制字段。"""
        config = CapabilityConfig()
        assert config.capability_request_max_tokens == 600
        assert config.capability_bundle_max_tokens == 3000
        assert config.skill_card_max_tokens == 0  # 不限制

    def test_capability_config_timeout_hierarchy(self):
        """验证默认不靠超时层级限制子代理。"""
        config = CapabilityConfig()
        assert config.subagent_heartbeat_timeout == 0
        assert config.subagent_run_timeout == 0

    def test_capability_config_evidence_requirements(self):
        """验证证据要求字段。"""
        config = CapabilityConfig()
        assert config.subagent_min_evidence_for_done == 1

    def test_capability_config_grant_expiration(self):
        """验证授权过期设置。"""
        config = CapabilityConfig()
        assert config.capability_grant_expires_after_task is True


class TestCapabilityConfigEdgeCases:
    """测试 CapabilityConfig 边界情况。"""

    def test_capability_config_all_zero_limits(self):
        """验证全 0 限制值。"""
        config = CapabilityConfig(
            capability_request_max_tried_items=0,
            capability_request_max_evidence_items=0,
            capability_request_max_per_task=0,
            capability_grant_max_skills=0,
            capability_grant_max_tools=0,
            skill_card_max_tokens=0,
            tool_card_max_tokens=0,
            skill_body_max_tokens=0,
        )
        # 全 0 表示不限制
        assert config.capability_request_max_tried_items == 0
        assert config.capability_grant_max_skills == 0

    def test_capability_config_custom_limits(self):
        """验证自定义限制值。"""
        config = CapabilityConfig(
            capability_candidate_limit=10,
            capability_bundle_max_tokens=5000,
            capability_alternative_max_attempts=5,
        )
        assert config.capability_candidate_limit == 10
        assert config.capability_bundle_max_tokens == 5000
        assert config.capability_alternative_max_attempts == 5
