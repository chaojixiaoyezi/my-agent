"""Tests for settings/config_normalize.py: config normalization, old field compatibility, and error messages.

给人看的解释：
测试配置归一化模块：配置归一化、旧字段兼容、错误提示。
"""
import tempfile
from pathlib import Path

import pytest

from agent_py_agent.agent.settings.config import AgentConfig, load_simple_yaml
from agent_py_agent.agent.settings.config_normalize import (
    normalize_agent_config,
    normalize_subagent_workflow_config,
)


class TestNormalizeAgentConfig:
    """测试 normalize_agent_config 配置归一化。"""

    def test_normalize_valid_model_backend_echo(self):
        """验证有效的 echo 后端。"""
        data = {"model_backend": "echo"}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["model_backend"] == "echo"
        assert len(warnings) == 0

    def test_normalize_valid_model_backend_openai(self):
        """验证有效的 openai_compatible 后端。"""
        data = {"model_backend": "openai_compatible"}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["model_backend"] == "openai_compatible"
        assert len(warnings) == 0

    def test_normalize_invalid_model_backend_falls_back(self):
        """验证无效后端回退到默认值。"""
        data = {"model_backend": "invalid"}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["model_backend"] == "echo"  # 默认值
        assert len(warnings) > 0

    def test_normalize_request_timeout_valid(self):
        """验证有效的 request_timeout。"""
        data = {"request_timeout": 120}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["request_timeout"] == 120
        assert len(warnings) == 0

    def test_normalize_request_timeout_too_low(self):
        """验证过小的 request_timeout 回退。"""
        data = {"request_timeout": 0}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["request_timeout"] == 60  # 默认值
        assert len(warnings) > 0

    def test_normalize_request_timeout_too_high(self):
        """验证过大的 request_timeout 回退。"""
        data = {"request_timeout": 999}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["request_timeout"] == 60  # 默认值（上限600）
        assert len(warnings) > 0

    def test_normalize_max_tokens_valid(self):
        """验证有效的 max_tokens。"""
        data = {"max_tokens": 2048}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["max_tokens"] == 2048
        assert len(warnings) == 0

    def test_normalize_max_tokens_invalid(self):
        """验证无效的 max_tokens 回退。"""
        data = {"max_tokens": -100}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["max_tokens"] == 1024  # 默认值
        assert len(warnings) > 0

    def test_normalize_temperature_valid(self):
        """验证有效的 temperature。"""
        data = {"temperature": "0.7"}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["temperature"] == "0.7"
        assert len(warnings) == 0

    def test_normalize_temperature_too_high(self):
        """验证过高的 temperature 回退。"""
        data = {"temperature": "3.0"}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["temperature"] == "0.2"  # 默认值
        assert len(warnings) > 0

    def test_normalize_temperature_negative(self):
        """验证负数 temperature 回退。"""
        data = {"temperature": "-1.0"}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["temperature"] == "0.2"  # 默认值
        assert len(warnings) > 0

    def test_normalize_gateway_stale_seconds_valid(self):
        """验证有效的 gateway_stale_seconds。"""
        data = {"gateway_stale_seconds": 300}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["gateway_stale_seconds"] == 300
        assert len(warnings) == 0

    def test_normalize_gateway_stale_seconds_too_low(self):
        """验证过小的 gateway_stale_seconds 回退。"""
        data = {"gateway_stale_seconds": 10}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["gateway_stale_seconds"] == 120  # 默认值（最小30）
        assert len(warnings) > 0

    def test_normalize_max_tool_rounds_valid(self):
        """验证有效的 max_tool_rounds。"""
        data = {"max_tool_rounds": 10}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["max_tool_rounds"] == 10
        assert len(warnings) == 0

    def test_normalize_max_tool_rounds_zero_means_unlimited(self):
        """验证 max_tool_rounds=0 表示不限制。"""
        data = {"max_tool_rounds": 0}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["max_tool_rounds"] == 0
        assert len(warnings) == 0

    def test_normalize_max_tool_rounds_invalid(self):
        """验证无效的 max_tool_rounds 回退。"""
        data = {"max_tool_rounds": -1}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["max_tool_rounds"] == 0  # 默认值，0 表示不限制
        assert len(warnings) > 0

    def test_normalize_memory_top_k_valid(self):
        """验证有效的 memory_top_k。"""
        data = {"memory_top_k": 10}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["memory_top_k"] == 10
        assert len(warnings) == 0

    def test_normalize_subagent_allowed_tools_list(self):
        """验证子代理默认工具白名单会被归一成字符串列表。"""
        data = {"subagent_allowed_tools": ["read_file", " write_file ", "", None]}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["subagent_allowed_tools"] == ["read_file", "write_file"]
        assert len(warnings) == 0

    def test_normalize_subagent_allowed_tools_scalar(self):
        """验证逗号分隔的子代理工具白名单也可用。"""
        data = {"subagent_allowed_tools": "read_file, write_file, append_file"}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["subagent_allowed_tools"] == ["read_file", "write_file", "append_file"]
        assert len(warnings) == 0

    def test_subagent_defaults_leave_decisions_automatic(self):
        """验证默认子代理配置不让用户预判工具和工作流，只保留宽松数量上限。"""
        normalized, warnings = normalize_agent_config({})
        assert warnings == []
        assert normalized["max_subagents"] == 1000
        assert normalized["subagent_allowed_tools"] == []
        assert normalized["subagent_role_template_dirs"] == []
        assert AgentConfig().subagent_workflow_mode == "auto"

    def test_empty_subagent_workflow_mode_means_auto(self):
        """验证配置文件里把工作流模式置空时，运行期按自动策略处理。"""
        config = AgentConfig()
        config.subagent_workflow_mode = ""
        warnings = normalize_subagent_workflow_config(config)
        assert warnings == []
        assert config.subagent_workflow_mode == "auto"

    def test_normalize_subagent_board_limit_invalid(self):
        """验证无效的 subagent_board_limit 会回退。"""
        data = {"subagent_board_limit": "many"}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["subagent_board_limit"] == AgentConfig().subagent_board_limit
        assert len(warnings) > 0

    def test_normalize_task_lock_timeout_seconds_invalid(self):
        """验证无效的 task_lock_timeout_seconds 会回退，避免配置样例变成假字段。"""
        data = {"task_lock_timeout_seconds": 0}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["task_lock_timeout_seconds"] == AgentConfig().task_lock_timeout_seconds
        assert len(warnings) > 0

    def test_normalize_runtime_bool_strings(self):
        """验证运行期布尔开关里的字符串 false 不会在业务代码里变成真值。"""
        data = {
            "audit_enabled": "false",
            "auto_save_memory": "false",
            "enable_subagents": "false",
            "watchdog_enabled": "true",
            "daemon_probe": "false",
            "auto_bench_model_on_first_use": "false",
        }
        normalized, warnings = normalize_agent_config(data)
        assert normalized["audit_enabled"] is False
        assert normalized["auto_save_memory"] is False
        assert normalized["enable_subagents"] is False
        assert normalized["watchdog_enabled"] is True
        assert normalized["daemon_probe"] is False
        assert normalized["auto_bench_model_on_first_use"] is False
        assert warnings == []

    def test_normalize_empty_dict(self):
        """验证空字典使用所有默认值。"""
        normalized, warnings = normalize_agent_config({})
        assert normalized["model_backend"] == "echo"
        assert normalized["request_timeout"] == 60
        assert len(warnings) == 0


class TestNormalizeSubagentWorkflowConfig:
    """测试 normalize_subagent_workflow_config 子代理工作流配置归一化。"""

    def test_normalize_workflow_mode_off(self):
        """验证 off 模式保持不变。"""
        config = AgentConfig()
        config.subagent_workflow_mode = "off"
        warnings = normalize_subagent_workflow_config(config)
        assert config.subagent_workflow_mode == "off"

    def test_normalize_workflow_mode_auto(self):
        """验证 auto 模式保持不变。"""
        config = AgentConfig()
        config.subagent_workflow_mode = "auto"
        warnings = normalize_subagent_workflow_config(config)
        assert config.subagent_workflow_mode == "auto"

    def test_normalize_workflow_mode_manual(self):
        """验证 manual 模式保持不变。"""
        config = AgentConfig()
        config.subagent_workflow_mode = "manual"
        warnings = normalize_subagent_workflow_config(config)
        assert config.subagent_workflow_mode == "manual"

    def test_normalize_workflow_mode_invalid(self):
        """验证无效模式回退到默认值。"""
        config = AgentConfig()
        config.subagent_workflow_mode = "unknown"
        warnings = normalize_subagent_workflow_config(config)
        assert config.subagent_workflow_mode == "auto"  # 默认值
        assert len(warnings) > 0

    def test_normalize_builtin_workflows_true(self):
        """验证 builtin_workflows 为 true。"""
        config = AgentConfig()
        config.subagent_builtin_workflows = True
        normalize_subagent_workflow_config(config)
        assert config.subagent_builtin_workflows is True

    def test_normalize_builtin_workflows_false(self):
        """验证 builtin_workflows 为 false。"""
        config = AgentConfig()
        config.subagent_builtin_workflows = False
        normalize_subagent_workflow_config(config)
        assert config.subagent_builtin_workflows is False

    def test_normalize_review_rounds_valid(self):
        """验证有效的 review_rounds。"""
        config = AgentConfig()
        config.subagent_workflow_review_rounds = 3
        normalize_subagent_workflow_config(config)
        assert config.subagent_workflow_review_rounds == 3

    def test_normalize_review_rounds_out_of_range(self):
        """验证超出范围的 review_rounds 回退到默认值。"""
        config = AgentConfig()
        config.subagent_workflow_review_rounds = 10
        normalize_subagent_workflow_config(config)
        assert config.subagent_workflow_review_rounds == 1  # 默认值


class TestNormalizeAgentConfigIntegration:
    """测试 normalize_agent_config 和 normalize_subagent_workflow_config 集成。"""

    def test_default_config_yaml_matches_public_agent_config_fields(self):
        """默认配置样例必须覆盖所有公开配置字段，且不能包含会被忽略的假字段。"""
        config_path = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
        yaml_keys = set(load_simple_yaml(config_path))
        internal_keys = {
            "memory_config_warnings",
            "subagent_workflow_config_warnings",
            "config_warnings",
        }
        config_keys = set(AgentConfig.__dataclass_fields__) - internal_keys

        assert sorted(config_keys - yaml_keys) == []
        assert sorted(yaml_keys - config_keys) == []

    def test_both_normalizations_together(self):
        """验证两个归一化一起使用。"""
        data = {
            "model_backend": "echo",
            "max_tool_rounds": 10,
        }
        normalized, warnings = normalize_agent_config(data)
        assert normalized["model_backend"] == "echo"
        assert normalized["max_tool_rounds"] == 10


class TestConfigWarnings:
    """测试配置警告生成。"""

    def test_warnings_list_populated_for_invalid_values(self):
        """验证无效值产生警告。"""
        data = {
            "model_backend": "invalid",
            "max_tool_rounds": -1,
        }
        normalized, warnings = normalize_agent_config(data)
        assert len(warnings) >= 2  # 至少两个警告

    def test_warnings_list_empty_for_valid_values(self):
        """验证有效值不产生警告。"""
        data = {
            "model_backend": "echo",
            "max_tool_rounds": 10,
        }
        normalized, warnings = normalize_agent_config(data)
        assert len(warnings) == 0

    def test_warning_message_contains_field_name(self):
        """验证警告消息包含字段名。"""
        data = {"model_backend": "bad_backend"}
        normalized, warnings = normalize_agent_config(data)
        assert any("model_backend" in w for w in warnings)

    def test_warning_message_contains_fallback_value(self):
        """验证警告消息包含回退值。"""
        data = {"model_backend": "bad"}
        normalized, warnings = normalize_agent_config(data)
        assert any("echo" in w for w in warnings)
