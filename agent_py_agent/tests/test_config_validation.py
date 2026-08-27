from __future__ import annotations

"""配置解析验证测试。"""

from pathlib import Path

from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.config import AgentConfig, normalize_agent_config


def _write_config(tmp_path, lines: list[str]):
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text("\n".join(lines), encoding="utf-8")
    return config_path


def test_normalize_agent_config_valid():
    data = {
        "model_backend": "echo",
        "request_timeout": "30",
        "max_tokens": "1024",
        "temperature": "0.7",
        "gateway_heartbeat_interval": "10",
        "gateway_stale_seconds": "120",
    }
    normalized, warnings = normalize_agent_config(data)
    assert warnings == []
    assert normalized["model_backend"] == "echo"
    assert normalized["request_timeout"] == 30
    assert normalized["max_tokens"] == 1024
    assert normalized["temperature"] == "0.7"
    assert normalized["gateway_heartbeat_interval"] == 10
    assert normalized["gateway_stale_seconds"] == 120


def test_agent_config_default_max_tokens_matches_shipped_config():
    shipped = load_config(Path(__file__).parents[1] / "config" / "agent_config.yaml")
    assert AgentConfig().max_tokens == shipped.max_tokens
    assert (
        AgentConfig().anthropic_prompt_cache_enabled
        is shipped.anthropic_prompt_cache_enabled
        is True
    )
    assert (
        AgentConfig().task_progress_closeout_repair_attempts
        == shipped.task_progress_closeout_repair_attempts
        == 1
    )


def test_card_and_prompt_defaults_match_shipped_config():
    config_path = Path(__file__).parents[1] / "config" / "agent_config.yaml"
    shipped = load_config(config_path)
    defaults = AgentConfig()
    assert defaults.prompt_files == shipped.prompt_files == ["builtin:prompts/default.md"]
    assert defaults.feishu_session_lock_enabled is shipped.feishu_session_lock_enabled is True
    assert defaults.feishu_personal_idle_lock_seconds == shipped.feishu_personal_idle_lock_seconds == 10800
    assert defaults.feishu_connection_mode == shipped.feishu_connection_mode == "long_connection"
    assert defaults.gateway_per_user_owner_scoping is shipped.gateway_per_user_owner_scoping is True
    assert defaults.tool_catalog_deferred_categories == shipped.tool_catalog_deferred_categories == [
        "collaboration",
        "goal",
        "web",
        "vision",
        "meta",
        "mcp",
    ]
    assert defaults.tool_catalog_include_examples is shipped.tool_catalog_include_examples is False
    assert defaults.tool_catalog_entry_max_chars == shipped.tool_catalog_entry_max_chars == 700
    assert config_path.read_text(encoding="utf-8").count("gateway_per_user_owner_scoping:") == 1


def test_terminal_tool_fold_defaults_match_shipped_config() -> None:
    config_path = Path(__file__).parents[1] / "config" / "agent_config.yaml"
    shipped = load_config(config_path)
    defaults = AgentConfig()

    assert (
        defaults.conversation_terminal_tool_fold_enabled
        is shipped.conversation_terminal_tool_fold_enabled
        is True
    )
    assert (
        defaults.conversation_terminal_tool_fold_max_chars
        == shipped.conversation_terminal_tool_fold_max_chars
        == 6_000
    )
    assert (
        defaults.conversation_terminal_tool_hot_tail_seconds
        == shipped.conversation_terminal_tool_hot_tail_seconds
        == 300
    )


def test_terminal_tool_fold_config_is_normalized_and_bounded() -> None:
    normalized, warnings = normalize_agent_config(
        {
            "conversation_terminal_tool_fold_enabled": "false",
            "conversation_terminal_tool_fold_max_chars": "2400",
            "conversation_terminal_tool_hot_tail_seconds": "120",
        }
    )

    assert warnings == []
    assert normalized["conversation_terminal_tool_fold_enabled"] is False
    assert normalized["conversation_terminal_tool_fold_max_chars"] == 2_400
    assert normalized["conversation_terminal_tool_hot_tail_seconds"] == 120

    fallback, warnings = normalize_agent_config(
        {
            "conversation_terminal_tool_fold_max_chars": "999999",
            "conversation_terminal_tool_hot_tail_seconds": "999999",
        }
    )

    assert any("conversation_terminal_tool_fold_max_chars" in item for item in warnings)
    assert (
        fallback["conversation_terminal_tool_fold_max_chars"]
        == AgentConfig().conversation_terminal_tool_fold_max_chars
    )
    assert any("conversation_terminal_tool_hot_tail_seconds" in item for item in warnings)
    assert (
        fallback["conversation_terminal_tool_hot_tail_seconds"]
        == AgentConfig().conversation_terminal_tool_hot_tail_seconds
    )


def test_normalize_agent_config_type_coercion():
    data = {
        "request_timeout": 45,
        "max_tokens": 2048,
        "temperature": 0.5,
    }
    normalized, warnings = normalize_agent_config(data)
    assert warnings == []
    assert normalized["request_timeout"] == 45
    assert normalized["max_tokens"] == 2048
    assert normalized["temperature"] == "0.5"


def test_normalize_agent_config_out_of_range():
    data = {
        "request_timeout": "0",
        "max_tokens": "-100",
        "gateway_heartbeat_interval": "2",
        "gateway_stale_seconds": "10",
    }
    normalized, warnings = normalize_agent_config(data)
    assert len(warnings) == 4
    assert any("request_timeout" in w for w in warnings)
    assert any("max_tokens" in w for w in warnings)
    assert any("gateway_heartbeat_interval" in w for w in warnings)
    assert any("gateway_stale_seconds" in w for w in warnings)
    # 应该回退到默认值
    defaults_normalized, _ = normalize_agent_config({})
    assert normalized["request_timeout"] == defaults_normalized["request_timeout"]
    assert normalized["max_tokens"] == defaults_normalized["max_tokens"]


def test_normalize_agent_config_unknown_keys(tmp_path):
    config_path = _write_config(
        tmp_path,
        [
            "model_backend: echo",
            "future_feature_enabled: true",
            "nonexistent_setting: abc",
        ],
    )
    config = load_config(config_path)
    assert any("future_feature_enabled" in w for w in config.config_warnings)
    assert any("nonexistent_setting" in w for w in config.config_warnings)
    assert config.model_backend == "echo"


def test_normalize_agent_config_invalid_model_backend():
    data = {"model_backend": "openai"}
    normalized, warnings = normalize_agent_config(data)
    assert any("model_backend" in w for w in warnings)
    assert normalized["model_backend"] == "echo"


def test_normalize_agent_config_temperature_range():
    # 正常温度
    data = {"temperature": "1.5"}
    normalized, warnings = normalize_agent_config(data)
    assert warnings == []
    assert normalized["temperature"] == "1.5"

    # 超出范围
    data = {"temperature": "3.0"}
    normalized, warnings = normalize_agent_config(data)
    assert any("temperature" in w for w in warnings)


def test_load_config_validates_and_keeps_warnings(tmp_path):
    config_path = _write_config(
        tmp_path,
        [
            "model_backend: echo",
            "request_timeout: 30",
            "max_tokens: 1024",
            "temperature: 0.7",
            "gateway_heartbeat_interval: 10",
            "gateway_stale_seconds: 120",
        ],
    )
    config = load_config(config_path)
    assert config.model_backend == "echo"
    assert config.request_timeout == 30
    assert config.max_tokens == 1024
    assert config.config_warnings == []


def test_load_config_warns_on_bad_values(tmp_path):
    config_path = _write_config(
        tmp_path,
        [
            "model_backend: invalid_backend",
            "request_timeout: 0",
            "temperature: 5.0",
            "unknown_future_key: true",
        ],
    )
    config = load_config(config_path)
    assert config.model_backend == "echo"  # default
    assert config.request_timeout == 240  # default
    assert len(config.config_warnings) >= 3  # backend + timeout + temperature + unknown key


def test_load_config_coerces_string_numbers(tmp_path):
    config_path = _write_config(
        tmp_path,
        [
            "request_timeout: 120",
            "max_tokens: 2048",
        ],
    )
    config = load_config(config_path)
    assert config.request_timeout == 120
    assert config.max_tokens == 2048


def test_load_config_keeps_runner_timeout_by_role(tmp_path):
    config_path = _write_config(
        tmp_path,
        [
            'runner_timeout_by_role: {"root": "off", "coordinator": "off", "worker": 8}',
        ],
    )

    config = load_config(config_path)

    assert config.runner_timeout_by_role == {"root": "off", "coordinator": "off", "worker": 8}


def test_daemon_defaults_are_true():
    """验证 daemon_mutate_state 和 daemon_start_runners 默认值为 True。"""
    defaults_normalized, _ = normalize_agent_config({})
    assert defaults_normalized.get("daemon_mutate_state") is True, "daemon_mutate_state 默认应为 True"
    assert defaults_normalized.get("daemon_start_runners") is True, "daemon_start_runners 默认应为 True"


def test_acceptance_real_execution_config_defaults_are_conservative():
    """验证真实验收执行配置默认关闭，避免老验收路径自动跑命令。"""
    defaults_normalized, warnings = normalize_agent_config({})
    assert warnings == []
    assert defaults_normalized["result_check_execute_tests"] is False
    assert defaults_normalized["result_check_timeout_seconds"] == 120


def test_acceptance_real_execution_config_coercion_and_range():
    """验证真实验收执行配置支持显式开启和超时校验。"""
    normalized, warnings = normalize_agent_config({
        "result_check_execute_tests": "true",
        "result_check_timeout_seconds": "30",
    })
    assert warnings == []
    assert normalized["result_check_execute_tests"] is True
    assert normalized["result_check_timeout_seconds"] == 30

    normalized_default, warnings = normalize_agent_config({
        "result_check_timeout_seconds": "9999",
    })
    defaults_normalized, _ = normalize_agent_config({})
    assert any("result_check_timeout_seconds" in warning for warning in warnings)
    assert normalized_default["result_check_timeout_seconds"] == defaults_normalized["result_check_timeout_seconds"]


def test_subagent_debug_trace_level_defaults_to_off():
    """验证子代理调试追踪默认关闭，避免普通运行写额外测试日志。"""
    defaults_normalized, warnings = normalize_agent_config({})
    assert warnings == []
    assert defaults_normalized["subagent_debug_trace_level"] == 0


def test_subagent_debug_trace_level_accepts_zero_to_five():
    """验证子代理调试追踪等级只接受 0-5，方便测试期开不同详细度。"""
    normalized, warnings = normalize_agent_config({"subagent_debug_trace_level": "5"})
    assert warnings == []
    assert normalized["subagent_debug_trace_level"] == 5

    normalized_default, warnings = normalize_agent_config({"subagent_debug_trace_level": "6"})
    defaults_normalized, _ = normalize_agent_config({})
    assert any("subagent_debug_trace_level" in warning for warning in warnings)
    assert normalized_default["subagent_debug_trace_level"] == defaults_normalized["subagent_debug_trace_level"]


def test_subagent_memory_policy_config_is_normalized_without_closed_enum():
    """验证子代理记忆保留策略可配置，策略名不做封闭枚举硬卡。"""
    normalized, warnings = normalize_agent_config({
        "subagent_memory_retention_policy": "custom-cleanup-after-parent-review",
        "subagent_memory_delete_after_days": "14",
        "subagent_destroy_summary_required": "false",
    })

    assert warnings == []
    assert normalized["subagent_memory_retention_policy"] == "custom-cleanup-after-parent-review"
    assert normalized["subagent_memory_delete_after_days"] == 14
    assert normalized["subagent_destroy_summary_required"] is False

    normalized_default, warnings = normalize_agent_config({
        "subagent_memory_delete_after_days": "-1",
    })
    defaults_normalized, _ = normalize_agent_config({})
    assert any("subagent_memory_delete_after_days" in warning for warning in warnings)
    assert normalized_default["subagent_memory_delete_after_days"] == defaults_normalized["subagent_memory_delete_after_days"]


def test_lease_config_defaults():
    """验证 lease 心跳续期配置项的默认值和 coerce 规则。"""
    defaults_normalized, _ = normalize_agent_config({})
    assert defaults_normalized.get("lease_heartbeat_interval_seconds") == 60
    assert defaults_normalized.get("lease_stale_without_heartbeat_seconds") == 300


def test_lease_config_coercion():
    """验证 lease 配置项能接受合法值并拒绝越界值。"""
    data = {
        "lease_heartbeat_interval_seconds": "30",
        "lease_stale_without_heartbeat_seconds": "600",
    }
    normalized, warnings = normalize_agent_config(data)
    assert warnings == []
    assert normalized["lease_heartbeat_interval_seconds"] == 30
    assert normalized["lease_stale_without_heartbeat_seconds"] == 600


def test_lease_config_out_of_range():
    """验证 lease 配置项对过小值会报警并回退到默认值。"""
    data = {
        "lease_heartbeat_interval_seconds": "5",
        "lease_stale_without_heartbeat_seconds": "10",
    }
    normalized, warnings = normalize_agent_config(data)
    assert len(warnings) == 2
    defaults_normalized, _ = normalize_agent_config({})
    assert normalized["lease_heartbeat_interval_seconds"] == defaults_normalized["lease_heartbeat_interval_seconds"]
    assert normalized["lease_stale_without_heartbeat_seconds"] == defaults_normalized["lease_stale_without_heartbeat_seconds"]
