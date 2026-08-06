from __future__ import annotations


def test_runtime_guard_file_contains_tool_repeat_guard_defaults():
    from agent_py_agent.agent.agent_core.tool_guard.call_guardrail_config import (
        readonly_no_progress_threshold,
        repeat_fail_threshold,
        terminal_block_enabled,
    )

    class Params:
        task_attributes = {}

    assert repeat_fail_threshold(Params()) == 10
    assert readonly_no_progress_threshold(Params()) == 3
    assert terminal_block_enabled(Params()) is False


def test_runtime_guard_file_contains_tool_rate_limit_defaults():
    from agent_py_agent.agent.tooling.registry_rate_limit_policy import tool_rate_limit_policy

    policy = tool_rate_limit_policy(None)

    assert policy is not None
    assert policy.window_seconds == 60.0
    assert policy.max_calls == 60
    assert policy.failure_threshold == 3
    assert policy.backoff_schedule_seconds == (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
    assert policy.max_records == 256


def test_tool_rate_limit_boundary_policy_overrides_shared_defaults():
    from agent_py_agent.agent.tooling.registry_rate_limit_policy import tool_rate_limit_policy

    policy = tool_rate_limit_policy(
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


def test_runtime_guard_file_contains_tool_agent_budget_defaults():
    from agent_py_agent.agent.agent_core.tool_guard.agent_budget import _budget_int
    from agent_py_agent.agent.settings.config_io import load_simple_yaml
    from agent_py_agent.agent.settings.runtime_guard_config import (
        DEFAULT_RUNTIME_GUARD_CONFIG_PATH,
    )

    class Config:
        pass

    defaults = load_simple_yaml(DEFAULT_RUNTIME_GUARD_CONFIG_PATH)

    assert _budget_int(Config(), "tool_agent_budget_window_seconds") == int(defaults["tool_agent_budget_window_seconds"])
    assert _budget_int(Config(), "tool_agent_budget_max_calls") == int(defaults["tool_agent_budget_max_calls"])


def test_runtime_guard_file_contains_tool_loop_and_runner_defaults():
    from agent_py_agent.agent.agent_core._tool_loop_service import _effective_max_tool_rounds
    from agent_py_agent.agent.agent_core.runner.dispatch import (
        _runner_max_attempts,
        _same_run_redispatch_limit,
    )
    from agent_py_agent.agent.settings.config_io import load_simple_yaml
    from agent_py_agent.agent.settings.runtime_guard_config import (
        DEFAULT_RUNTIME_GUARD_CONFIG_PATH,
    )

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


def test_runner_retry_limit_only_uses_off_or_zero_to_disable():
    from agent_py_agent.agent.agent_core.runner.dispatch import _runner_max_attempts
    from agent_py_agent.agent.settings.config_io import load_simple_yaml
    from agent_py_agent.agent.settings.runtime_guard_config import (
        DEFAULT_RUNTIME_GUARD_CONFIG_PATH,
    )

    default_limit = int(load_simple_yaml(DEFAULT_RUNTIME_GUARD_CONFIG_PATH)["runner_failure_retry_limit"])

    assert _runner_max_attempts("off") == 0
    assert _runner_max_attempts("0") == 0
    for old_alias in ("none", "disabled", "false", "no"):
        assert _runner_max_attempts(old_alias) == default_limit


def test_agent_config_blank_tool_rounds_disables_hidden_runtime_default():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._tool_loop_service import _effective_max_tool_rounds
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleNamespace(config=AgentConfig(enable_tools=True, memory_path="memory.jsonl"))
    params = SimpleNamespace(task_attributes={})

    assert _effective_max_tool_rounds(agent, params) == 0


def test_runtime_guard_readers_follow_the_same_patched_yaml(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._tool_loop_service import _effective_max_tool_rounds
    from agent_py_agent.agent.agent_core.runner.dispatch import (
        _runner_max_attempts,
        _same_run_redispatch_limit,
    )
    from agent_py_agent.agent.agent_core.tool_guard.agent_budget import _budget_int
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

    agent = SimpleNamespace(config=SimpleNamespace())
    params = SimpleNamespace(task_attributes={})
    config = SimpleNamespace()

    assert _effective_max_tool_rounds(agent, params) == 17
    assert _runner_max_attempts("auto") == 5
    assert _same_run_redispatch_limit(None) == 4
    assert _budget_int(config, "tool_agent_budget_window_seconds") == 33
    assert _budget_int(config, "tool_agent_budget_max_calls") == 44


def test_runtime_guard_readers_prefer_agent_policy_snapshot():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._tool_loop_service import _effective_max_tool_rounds
    from agent_py_agent.agent.agent_core.runner.dispatch import (
        _runner_max_attempts,
        _same_run_redispatch_limit,
    )
    from agent_py_agent.agent.agent_core.tool_guard.agent_budget import _budget_int
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy

    policy = RuntimeGuardPolicy(
        values={
            "max_tool_rounds": 21,
            "runner_failure_retry_limit": 6,
            "same_run_redispatch_limit": 5,
            "tool_agent_budget_window_seconds": 77,
        },
        sources={},
    )
    agent = SimpleNamespace(config=SimpleNamespace(), runtime_guard_policy=policy)
    params = SimpleNamespace(task_attributes={})
    config = SimpleNamespace()

    assert _effective_max_tool_rounds(agent, params) == 21
    assert _runner_max_attempts("auto", runtime_policy=policy) == 6
    assert _same_run_redispatch_limit(None, runtime_policy=policy) == 5
    assert _budget_int(config, "tool_agent_budget_window_seconds", policy=policy) == 77


def test_registry_runtime_gate_policy_uses_passed_runtime_policy():
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy
    from agent_py_agent.agent.tooling.action_policy import _tool_guardrail_config
    from agent_py_agent.agent.tooling.registry_rate_limit_policy import tool_rate_limit_policy

    policy = RuntimeGuardPolicy(
        values={
            "repeat_fail_threshold": 13,
            "readonly_no_progress_threshold": 4,
            "terminal_block_enabled": True,
            "tool_rate_max_calls": 14,
            "tool_rate_window_seconds": 15,
            "tool_circuit_failure_threshold": 16,
            "tool_circuit_backoff_seconds": [1, 3, 5],
            "tool_rate_max_records": 17,
        },
        sources={},
    )

    guardrail = _tool_guardrail_config(None, policy)
    rate = tool_rate_limit_policy(None, policy)

    assert guardrail.repeat_fail_threshold == 13
    assert guardrail.readonly_no_progress_threshold == 4
    assert guardrail.terminal_block_enabled is True
    assert rate.max_calls == 14
    assert rate.window_seconds == 15.0
    assert rate.failure_threshold == 16
    assert rate.backoff_schedule_seconds == (1.0, 3.0, 5.0)
    assert rate.max_records == 17
