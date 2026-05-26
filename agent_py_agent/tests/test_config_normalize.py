"""Tests for settings/config_normalize.py: config normalization, old field compatibility, and error messages.

给人看的解释：
测试配置归一化模块：配置归一化、旧字段兼容、错误提示。
"""
import tempfile
from pathlib import Path

import pytest

from agent_py_agent.agent.settings.config import (
    HIDDEN_COMPAT_CONFIG_FIELDS,
    AgentConfig,
    load_simple_yaml,
)
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
        assert normalized["request_timeout"] == 240  # 默认值
        assert len(warnings) > 0

    def test_normalize_request_timeout_too_high(self):
        """验证过大的 request_timeout 回退。"""
        data = {"request_timeout": 999}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["request_timeout"] == 240  # 默认值（上限600）
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


class TestNormalizeSubagentAgentConfig:
    """测试 normalize_agent_config 里的子代理和用户空间配置。"""

    def test_normalize_subagent_allowed_tools_list(self):
        """验证子代理默认工具白名单会被归一成字符串列表。"""
        data = {"subagent_allowed_tools": ["read_file", " write_file ", "", None]}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["subagent_allowed_tools"] == ["read_file", "write_file"]
        assert len(warnings) == 0

    def test_normalize_subagent_allowed_tools_scalar(self):
        """验证逗号分隔的子代理工具白名单也可用。"""
        data = {"subagent_allowed_tools": "read_file, write_file, apply_patch"}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["subagent_allowed_tools"] == ["read_file", "write_file", "apply_patch"]
        assert len(warnings) == 0

    def test_subagent_defaults_leave_decisions_automatic(self):
        """验证默认子代理配置不让用户预判工具和工作流，只保留宽松数量上限。"""
        normalized, warnings = normalize_agent_config({})
        assert warnings == []
        assert normalized["max_subagents"] == 1000
        assert normalized["subagent_allowed_tools"] == []
        assert normalized["subagent_role_template_dirs"] == []
        assert normalized["subagent_mode"] == "trusted_local_hardening"
        assert AgentConfig().subagent_workflow_mode == "auto"

    def test_normalize_subagent_mode_invalid_falls_back(self):
        """验证子代理模式只有少量稳定档位，非法值回退到本地硬化默认。"""
        normalized, warnings = normalize_agent_config({"subagent_mode": "tiny_locked_down"})
        assert normalized["subagent_mode"] == "trusted_local_hardening"
        assert any("subagent_mode" in warning for warning in warnings)

    def test_default_config_exposes_fewer_than_ten_subagent_user_knobs(self):
        """验证默认配置不再暴露大量子代理微调参数，避免用户被奇葩参数拖住。"""
        config_path = Path(__file__).parents[1] / "config" / "agent_config.yaml"
        visible = load_simple_yaml(config_path)
        exposed = {
            key
            for key in visible
            if key.startswith("subagent_")
            or key in {"enable_subagents", "max_subagents", "acceptance_execute_tests", "acceptance_test_timeout_seconds"}
        }
        assert exposed == {
            "enable_subagents",
            "subagent_mode",
            "subagent_debug_trace_level",
            "max_subagents",
            "subagent_workspace",
            "subagent_role_template_dirs",
            "acceptance_execute_tests",
            "acceptance_test_timeout_seconds",
        }

    def test_hidden_subagent_compat_limits_default_to_unrestricted(self):
        """验证隐藏兼容参数默认不再限制层级、单次创建和接管链。"""
        defaults = AgentConfig()

        normalized, warnings = normalize_agent_config(
            {
                "subagent_hierarchy_default_max_depth": 0,
                "subagent_hierarchy_max_children_per_tool_call": 0,
                "subagent_takeover_chain_max_depth": 0,
            }
        )

        assert defaults.subagent_hierarchy_default_max_depth == 0
        assert defaults.subagent_hierarchy_max_children_per_tool_call == 0
        assert defaults.subagent_takeover_chain_max_depth == 0
        assert normalized["subagent_hierarchy_default_max_depth"] == 0
        assert normalized["subagent_hierarchy_max_children_per_tool_call"] == 0
        assert normalized["subagent_takeover_chain_max_depth"] == 0
        assert warnings == []

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

    def test_normalize_home_provider_risk_fields(self):
        """验证 provider 空间风险配置可归一化，避免执行层写死危险默认值。"""
        normalized, warnings = normalize_agent_config(
            {
                "home_runtime_bootstrap_enabled": "true",
                "home_context_enabled": "false",
                "home_lesson_auto_read_limit": "4",
                "daily_memory_mirror_enabled": "true",
                "run_task_workspace_enabled": "false",
                "provider_space_default_max_storage_mb": "512",
                "provider_space_max_download_file_mb": "64",
                "provider_space_trash_retention_days": "45",
                "provider_space_destructive_actions_use_trash": "true",
            }
        )
        assert warnings == []
        assert normalized["home_runtime_bootstrap_enabled"] is True
        assert normalized["home_context_enabled"] is False
        assert normalized["home_lesson_auto_read_limit"] == 4
        assert normalized["daily_memory_mirror_enabled"] is True
        assert normalized["run_task_workspace_enabled"] is False
        assert normalized["provider_space_default_max_storage_mb"] == 512
        assert normalized["provider_space_max_download_file_mb"] == 64
        assert normalized["provider_space_trash_retention_days"] == 45
        assert normalized["provider_space_destructive_actions_use_trash"] is True

    def test_normalize_empty_dict(self):
        """验证空字典使用所有默认值。"""
        normalized, warnings = normalize_agent_config({})
        assert normalized["model_backend"] == "echo"
        assert normalized["request_timeout"] == 240
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
        config_keys = set(AgentConfig.__dataclass_fields__) - internal_keys - HIDDEN_COMPAT_CONFIG_FIELDS

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
