"""Tests for settings/normalize.py: config normalization and error messages.

给人看的解释：
测试配置归一化模块：配置归一化和错误提示。
"""
import tempfile
from pathlib import Path

import pytest

from agent_py_agent.agent.settings.config import (
    INTERNAL_RUNTIME_CONFIG_FIELDS,
    AgentConfig,
    load_simple_yaml,
)
from agent_py_agent.agent.settings.defaults import DEFAULT_MODEL_MAX_TOKENS
from agent_py_agent.agent.settings.normalize import (
    normalize_agent_config,
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
        assert normalized["max_tokens"] == DEFAULT_MODEL_MAX_TOKENS
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
        assert normalized["max_tool_rounds"] is None
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
        assert normalized["max_subagents"] == 50
        assert normalized["subagent_allowed_tools"] == []
        assert normalized["subagent_role_template_dirs"] == []
        assert normalized["subagent_mode"] == "trusted_local_hardening"

    def test_normalize_subagent_mode_invalid_falls_back(self):
        """验证子代理模式只有少量稳定档位，非法值回退到本地硬化默认。"""
        normalized, warnings = normalize_agent_config({"subagent_mode": "tiny_locked_down"})
        assert normalized["subagent_mode"] == "trusted_local_hardening"
        assert any("subagent_mode" in warning for warning in warnings)

    def test_default_config_exposes_current_subagent_runtime_knobs(self):
        """验证默认配置明示当前子代理运行参数，不靠隐藏兼容字段。"""
        config_path = Path(__file__).parents[1] / "config" / "agent_config.yaml"
        visible = load_simple_yaml(config_path)
        exposed = {
            key
            for key in visible
            if key.startswith("subagent_")
            or key in {
                "enable_subagents",
                "max_subagents",
                "task_max_subagents",
                "task_max_grandchildren",
                "result_check_execute_tests",
                "result_check_timeout_seconds",
            }
        }
        assert exposed == {
            "enable_subagents",
            "subagent_mode",
            "subagent_allowed_tools",
            "subagent_board_limit",
            "subagent_cli_default_limit",
            "subagent_context_summary_inline_json_chars",
            "subagent_context_summary_inline_text_chars",
            "subagent_debug_trace_level",
            "subagent_descendant_scan_limit",
            "subagent_memory_retention_policy",
            "subagent_memory_delete_after_days",
            "subagent_destroy_summary_required",
            "subagent_hierarchy_default_max_depth",
            "subagent_hierarchy_max_children_per_tool_call",
            "subagent_hierarchy_recovery_max_nodes",
            "subagent_probe_default_limit",
            "subagent_watch_interval_seconds",
            "subagent_spawn_default_count",
            "subagent_takeover_chain_max_depth",
            "max_subagents",
            "task_max_subagents",
            "task_max_grandchildren",
            "subagent_workspace",
            "subagent_role_template_dirs",
            "result_check_execute_tests",
            "result_check_timeout_seconds",
        }

    def test_subagent_hierarchy_limits_default_to_unrestricted(self):
        """验证层级、单次创建和接管链默认不收紧。"""
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

    def test_normalize_gateway_request_poll_interval_accepts_fractional_seconds(self):
        """gateway 请求 worker 的空闲轮询间隔支持小数秒，避免配置写了不生效。"""
        normalized, warnings = normalize_agent_config({"gateway_request_poll_interval": "0.2"})

        assert normalized["gateway_request_poll_interval"] == 0.2
        assert warnings == []

    def test_normalize_gateway_request_poll_interval_rejects_too_small_values(self):
        """过小轮询间隔回到默认值，避免误配造成本地空转。"""
        normalized, warnings = normalize_agent_config({"gateway_request_poll_interval": 0.01})

        assert normalized["gateway_request_poll_interval"] == AgentConfig().gateway_request_poll_interval
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
                "home_context_enabled": "false",
                "home_lesson_auto_read_limit": "4",
                "run_task_workspace_enabled": "false",
            }
        )
        assert warnings == []
        assert normalized["home_context_enabled"] is False
        assert normalized["home_lesson_auto_read_limit"] == 4
        assert normalized["run_task_workspace_enabled"] is False

    def test_path_access_mode_only_accepts_current_values(self):
        """验证路径访问策略只认 normal/full，不把旧写法静默升格。"""
        normalized, warnings = normalize_agent_config({"path_access_mode": "full-access"})
        assert normalized["path_access_mode"] == "normal"
        assert any("path_access_mode" in warning for warning in warnings)

        normalized, warnings = normalize_agent_config({"path_access_mode": "full"})
        assert normalized["path_access_mode"] == "full"
        assert warnings == []

    def test_normalize_empty_dict(self):
        """验证空字典使用所有默认值。"""
        normalized, warnings = normalize_agent_config({})
        assert normalized["model_backend"] == "echo"
        assert normalized["request_timeout"] == 240
        assert len(warnings) == 0


class TestNormalizeAgentConfigIntegration:
    """测试 normalize_agent_config 与默认配置样例的一致性。"""

    def test_default_config_yaml_matches_public_agent_config_fields(self):
        """默认配置样例必须覆盖所有公开配置字段，且不能包含会被忽略的假字段。"""
        config_path = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
        yaml_keys = set(load_simple_yaml(config_path))
        internal_keys = {
            "memory_config_warnings",
            "config_warnings",
        } | INTERNAL_RUNTIME_CONFIG_FIELDS
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

    def test_warning_message_contains_default_value(self):
        """验证警告消息包含默认值。"""
        data = {"model_backend": "bad"}
        normalized, warnings = normalize_agent_config(data)
        assert any("echo" in w for w in warnings)


class TestNormalizeGatewayWorkers:
    """R2 真实场景抓到的回归：worker 数下限误写成默认值 3，2 被静默钳回 3。"""

    def test_normalize_gateway_request_workers_accepts_small_counts(self):
        """1 和 2 都是合法 worker 数，不允许被钳到默认值。"""
        for value in (1, 2):
            normalized, warnings = normalize_agent_config({"gateway_request_workers": value})
            assert normalized["gateway_request_workers"] == value
            assert len(warnings) == 0

    def test_normalize_gateway_request_workers_rejects_zero(self):
        """0 个 worker 无法处理请求，回退默认并告警。"""
        normalized, warnings = normalize_agent_config({"gateway_request_workers": 0})
        assert normalized["gateway_request_workers"] == 10
        assert len(warnings) > 0
