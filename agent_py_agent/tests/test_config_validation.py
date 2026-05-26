from __future__ import annotations

"""配置解析验证测试。"""

from agent_py_agent.agent.config import load_config
from agent_py_agent.agent.settings.config import normalize_agent_config


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
    assert config.model_backend == "echo"  # fallback
    assert config.request_timeout == 240  # fallback
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


# LLM: runner_timeout_by_role must survive config normalization so E2E can bound leaf workers without killing root.
# 函数用途: 验证角色级 runner timeout 配置能从 YAML 加载到 AgentConfig。
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
    """验证 daemon_apply 和 daemon_execute_runners 默认值为 True。"""
    defaults_normalized, _ = normalize_agent_config({})
    assert defaults_normalized.get("daemon_apply") is True, "daemon_apply 默认应为 True"
    assert defaults_normalized.get("daemon_execute_runners") is True, "daemon_execute_runners 默认应为 True"


def test_acceptance_real_execution_config_defaults_are_conservative():
    """验证真实验收执行配置默认关闭，避免老验收路径自动跑命令。"""
    defaults_normalized, warnings = normalize_agent_config({})
    assert warnings == []
    assert defaults_normalized["result_check_execute_tests"] is False
    assert defaults_normalized["result_check_timeout_seconds"] == 120
    assert defaults_normalized["closeout_for_all_task_nodes"] is False


def test_acceptance_real_execution_config_coercion_and_range():
    """验证真实验收执行配置支持显式开启和超时校验。"""
    normalized, warnings = normalize_agent_config({
        "result_check_execute_tests": "true",
        "result_check_timeout_seconds": "30",
        "closeout_for_all_task_nodes": "true",
    })
    assert warnings == []
    assert normalized["result_check_execute_tests"] is True
    assert normalized["result_check_timeout_seconds"] == 30
    assert normalized["closeout_for_all_task_nodes"] is True

    fallback, warnings = normalize_agent_config({
        "result_check_timeout_seconds": "9999",
    })
    defaults_normalized, _ = normalize_agent_config({})
    assert any("result_check_timeout_seconds" in warning for warning in warnings)
    assert fallback["result_check_timeout_seconds"] == defaults_normalized["result_check_timeout_seconds"]


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

    fallback, warnings = normalize_agent_config({"subagent_debug_trace_level": "6"})
    defaults_normalized, _ = normalize_agent_config({})
    assert any("subagent_debug_trace_level" in warning for warning in warnings)
    assert fallback["subagent_debug_trace_level"] == defaults_normalized["subagent_debug_trace_level"]


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
