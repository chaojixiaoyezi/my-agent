from __future__ import annotations


# LLM: runtime guard knobs should share one default config file so operators do not chase scattered YAMLs.
# 函数用途: 验证探索、本地进展和交付返工默认都读同一个配置入口。
def test_runtime_guard_configs_share_one_default_file():
    from agent_py_agent.agent.agent_core.delivery_closeout_config import (
        DEFAULT_DELIVERY_CLOSEOUT_CONFIG_PATH,
    )
    from agent_py_agent.agent.agent_core.exploration_fuse_config import (
        DEFAULT_EXPLORATION_FUSE_CONFIG_PATH,
    )

    assert DEFAULT_EXPLORATION_FUSE_CONFIG_PATH.name == "runtime_guard_config.yaml"
    assert DEFAULT_DELIVERY_CLOSEOUT_CONFIG_PATH == DEFAULT_EXPLORATION_FUSE_CONFIG_PATH


# LLM: Tool repeat guard should read the shared runtime guard config without exposing scattered knobs.
# 函数用途: 验证工具重复失败门的默认配置集中在 runtime_guard_config.yaml，且默认只拦动作不杀任务。
def test_runtime_guard_file_contains_tool_repeat_guard_defaults():
    from agent_py_agent.agent.agent_core.tool_call_guardrail import (
        _repeat_fail_threshold,
        _terminal_block_enabled,
    )

    class Params:
        task_attributes = {}

    assert _repeat_fail_threshold(Params()) == 10
    assert _terminal_block_enabled(Params()) is False


# LLM: Tool rate-limit defaults should live beside the other runtime guard knobs.
# 函数用途: 验证工具工程限流和熔断默认值来自统一 runtime_guard_config.yaml，而不是散落在调用点。
def test_runtime_guard_file_contains_tool_rate_limit_defaults():
    from agent_py_agent.agent.tooling.registry_runtime_gate_pipeline import _tool_rate_limit_policy

    policy = _tool_rate_limit_policy(None)

    assert policy is not None
    assert policy.window_seconds == 60.0
    assert policy.max_calls == 60
    assert policy.failure_threshold == 3
    assert policy.backoff_schedule_seconds == (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
    assert policy.max_records == 256


# LLM: Explicit write-boundary policies should still override shared defaults for special tools or monitors.
# 函数用途: 验证长期监控等调用方仍可显式覆盖统一默认限流参数。
def test_tool_rate_limit_boundary_policy_overrides_shared_defaults():
    from agent_py_agent.agent.tooling.registry_runtime_gate_pipeline import _tool_rate_limit_policy

    policy = _tool_rate_limit_policy(
        {
            "tool_rate_limit_policy": {
                "max_calls": 12,
                "window_seconds": 60,
                "failure_threshold": 5,
                "backoff_schedule_seconds": [10, 20, 30],
                "max_records": 64,
            }
        }
    )

    assert policy.max_calls == 12
    assert policy.window_seconds == 60.0
    assert policy.failure_threshold == 5
    assert policy.backoff_schedule_seconds == (10.0, 20.0, 30.0)
    assert policy.max_records == 64


# LLM: Per-agent tool budget should share the same runtime guard YAML as other count gates.
# 函数用途: 验证子代理/runner 工具预算默认值也集中在 runtime_guard_config.yaml。
def test_runtime_guard_file_contains_tool_agent_budget_defaults():
    from agent_py_agent.agent.agent_core.runtime_guard_config import (
        DEFAULT_RUNTIME_GUARD_CONFIG_PATH,
    )
    from agent_py_agent.agent.agent_core.tool_agent_budget import _budget_int
    from agent_py_agent.agent.settings.config_io import load_simple_yaml

    class Config:
        pass

    defaults = load_simple_yaml(DEFAULT_RUNTIME_GUARD_CONFIG_PATH)

    assert _budget_int(Config(), "tool_agent_budget_window_seconds") == int(defaults["tool_agent_budget_window_seconds"])
    assert _budget_int(Config(), "tool_agent_budget_max_calls") == int(defaults["tool_agent_budget_max_calls"])


# LLM: Tool-loop and runner retry counts should share the same runtime guard YAML.
# 函数用途: 验证主工具轮上限、runner 失败重试和同 run 重派限制都集中在 runtime_guard_config.yaml。
def test_runtime_guard_file_contains_tool_loop_and_runner_defaults():
    from agent_py_agent.agent.agent_core._tool_loop_service import _effective_max_tool_rounds
    from agent_py_agent.agent.agent_core.runner_dispatch import (
        _runner_max_attempts,
        _same_run_redispatch_limit,
    )
    from agent_py_agent.agent.agent_core.runtime_guard_config import (
        DEFAULT_RUNTIME_GUARD_CONFIG_PATH,
    )
    from agent_py_agent.agent.settings.config_io import load_simple_yaml

    class Agent:
        class Config:
            pass

        config = Config()

    class Params:
        task_attributes = {}

    defaults = load_simple_yaml(DEFAULT_RUNTIME_GUARD_CONFIG_PATH)

    assert _effective_max_tool_rounds(Agent(), Params()) == int(defaults["max_tool_rounds"])
    assert _runner_max_attempts("auto") == int(defaults["runner_failure_retry_limit"])
    assert _same_run_redispatch_limit(None) == int(defaults["same_run_redispatch_limit"])


# LLM: runtime guard readers should honor YAML patches instead of imported hard-coded defaults.
# 函数用途: 验证改 runtime_guard_config.yaml 后，工具轮、runner 重试和同 run 重派限制会同步生效。
def test_runtime_guard_readers_follow_the_same_patched_yaml(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._tool_loop_service import _effective_max_tool_rounds
    from agent_py_agent.agent.agent_core.runner_dispatch import (
        _runner_max_attempts,
        _same_run_redispatch_limit,
    )
    from agent_py_agent.agent.agent_core.tool_agent_budget import _budget_int
    from agent_py_agent.agent.settings import runtime_guard_config

    config_path = tmp_path / "runtime_guard_config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "max_tool_rounds: 17",
                "runner_failure_retry_limit: 5",
                "same_run_redispatch_limit: 4",
                "tool_agent_budget_window_seconds: 33",
                "tool_agent_budget_max_calls: 44",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_guard_config, "DEFAULT_RUNTIME_GUARD_CONFIG_PATH", config_path)

    agent = SimpleNamespace(config=SimpleNamespace(max_tool_rounds=None))
    params = SimpleNamespace(task_attributes={})
    config = SimpleNamespace()

    assert _effective_max_tool_rounds(agent, params) == 17
    assert _runner_max_attempts("auto") == 5
    assert _same_run_redispatch_limit(None) == 4
    assert _budget_int(config, "tool_agent_budget_window_seconds") == 33
    assert _budget_int(config, "tool_agent_budget_max_calls") == 44
