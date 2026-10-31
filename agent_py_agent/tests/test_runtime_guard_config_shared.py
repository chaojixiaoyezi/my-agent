# LLM: 守卫默认迁到唯一 typed 部署策略，旧 YAML 不再参与；测试保留实际 gate 与预算行为断言。
# 模块用途: 验证同一守卫快照到达重复调用、工程限流与供应恢复消费者。
from __future__ import annotations


def test_runtime_guard_file_contains_tool_repeat_guard_defaults():
    from agent_py_agent.agent.agent_core.tool_guard.call_guardrail_config import (
        hard_failure_halt_enabled,
        hard_failure_halt_threshold,
        readonly_no_progress_threshold,
        repeat_fail_threshold,
        repeated_failure_halt_threshold,
        repeated_success_hint_threshold,
        terminal_block_enabled,
    )

    class Params:
        task_attributes = {}

    assert repeat_fail_threshold(Params()) == 10
    assert readonly_no_progress_threshold(Params()) == 3
    assert repeated_success_hint_threshold(Params()) == 5
    assert terminal_block_enabled(Params()) is False
    assert repeated_failure_halt_threshold(Params()) == 15
    assert hard_failure_halt_enabled(Params()) is False
    assert hard_failure_halt_threshold(Params()) == 15


# LLM: 宿主加载不接运行期覆写；测试使用直接 typed 冻结对象，不恢复第二个来源入口。
# 函数用途: 验证成功重复提醒读取本次快照及原动作属性语义。
def test_repeated_success_observation_defaults_and_frozen_overrides():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_guard.call_guardrail import tool_guardrail_policy
    from agent_py_agent.agent.agent_core.tool_guard.call_guardrail_config import (
        DEFAULT_REPEATED_SUCCESS_HINT_THRESHOLD_COUNT,
        repeated_success_hint_threshold,
    )
    from agent_py_agent.agent.contracts.gates.tool_guardrail import ToolGuardrailConfig
    from agent_py_agent.agent.settings.runtime_guard_config import (
        RuntimeGuardPolicy,
        runtime_guard_policy,
    )

    default_policy = runtime_guard_policy()
    assert default_policy.values["repeated_success_hint_threshold"] == 5
    assert DEFAULT_REPEATED_SUCCESS_HINT_THRESHOLD_COUNT == 5
    assert ToolGuardrailConfig().repeated_success_hint_threshold == 5
    params = SimpleNamespace(
        runtime_guard_policy=RuntimeGuardPolicy(repeated_success_hint_threshold=7),
        task_attributes={},
    )
    assert repeated_success_hint_threshold(params) == 7
    assert tool_guardrail_policy(params)["repeated_success_hint_threshold"] == 7
    params.task_attributes = {"repeated_success_hint_threshold": 2}
    assert repeated_success_hint_threshold(params) == 2
    params.task_attributes = {"repeated_success_hint_threshold": 0}
    assert repeated_success_hint_threshold(params) == 0


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

    # 任务边界只能沿保护方向收紧共享默认（640e478ee）：调用预算和退避取更严，较宽阈值与较小账本容量不生效。
    assert policy.max_calls == 12
    assert policy.window_seconds == 60.0
    assert policy.failure_threshold == 3
    assert policy.backoff_schedule_seconds == (10.0, 20.0, 30.0, 30.0, 30.0, 30.0)
    assert policy.max_records == 256


# LLM: 运行读取改用 typed 默认，不能把 AgentConfig 已退役副本复活进部署策略。
# 函数用途: 检查唯一 typed 守卫只持有自己的字段。
def test_runtime_guard_file_no_longer_carries_agent_config_copies():
    """参数减量第 2 批：工具轮上限、单代理工具预算、runner 重跑次数只在 AgentConfig 一个家；
    守卫文件里这些副本在有 AgentConfig 时从来读不到，已删除。"""
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy

    defaults = RuntimeGuardPolicy().values

    for key in (
        "max_tool_rounds",
        "tool_agent_budget_window_seconds",
        "tool_agent_budget_max_calls",
        "runner_failure_retry_limit",
        "same_run_redispatch_limit",
    ):
        assert key not in defaults
    # 后台工作片预算与临时供应重派上限仍是守卫文件自己的概念。
    assert defaults["background_max_tool_rounds"] == 5000
    assert defaults["background_max_tool_calls_per_round"] == 4
    assert defaults["provider_transient_redispatch_limit"] == 8


# LLM: 旧文件存在也不能替代唯一宿主 JSON；工具轮与代理预算仍由原 AgentConfig/代码规则负责。
# 函数用途: 验证旧 YAML 的重复副本不会影响真实预算 gate。
def test_tool_rounds_and_agent_budget_ignore_patched_guard_yaml(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._tool_loop_service import (
        _DEFAULT_MAX_TOOL_ROUND_COUNT,
        _effective_max_tool_rounds,
    )
    from agent_py_agent.agent.agent_core.tool_guard.agent_budget import (
        ToolAgentBudgetRequest,
        check_tool_agent_budget,
    )
    config_path = tmp_path / "runtime_guard_config.yaml"
    config_path.write_text("max_tool_rounds: 17\ntool_agent_budget_max_calls: 1\n", encoding="utf-8")
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path))

    agent = SimpleNamespace(config=SimpleNamespace())
    params = SimpleNamespace(task_attributes={})

    assert _effective_max_tool_rounds(agent, params) == _DEFAULT_MAX_TOOL_ROUND_COUNT
    for index in range(3):
        assert check_tool_agent_budget(ToolAgentBudgetRequest(agent, "run-1", "read_file", now=float(index))) is None


# LLM: 直接构造同样走 typed 验证；冻结策略优先于默认，不恢复旧 values 字典入口。
# 函数用途: 检查供应重派预算读取当前策略快照。
def test_provider_supply_retry_limit_prefers_agent_policy_snapshot():
    from agent_py_agent.agent.agent_core.runner.dispatch import _provider_supply_retry_limit
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy

    policy = RuntimeGuardPolicy(provider_transient_redispatch_limit=5)

    assert _provider_supply_retry_limit("transient_error", runtime_policy=policy) == 5
    assert _provider_supply_retry_limit("transient_error") == 8
    assert _provider_supply_retry_limit("runner_error", runtime_policy=policy) == 0


def test_agent_config_blank_tool_rounds_defaults_to_extreme_guard():
    """LLM: 默认极高级别轮数保护(5000)——正常深度任务(如 5 万行项目复刻)几百轮
    远够,5000 等效不截停主循环,但真失控死循环仍有最后硬顶。普通可恢复工具
    失败只返给模型换路；action 级重复调用门、unknown_command_budget、compact
    防抖共同兜底。显式正数仍可限制,显式 0 才是不限制;任务属性可覆盖。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._tool_loop_service import (
        _DEFAULT_MAX_TOOL_ROUND_COUNT,
        _effective_max_tool_rounds,
    )
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleNamespace(config=AgentConfig(enable_tools=True, memory_path="memory.jsonl"))
    params = SimpleNamespace(task_attributes={})

    assert _DEFAULT_MAX_TOOL_ROUND_COUNT == 5000
    assert _effective_max_tool_rounds(agent, params) == _DEFAULT_MAX_TOOL_ROUND_COUNT
    # 显式 0 保留「不限制」逃生。
    agent_explicit_0 = SimpleNamespace(
        config=AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=0)
    )
    assert _effective_max_tool_rounds(agent_explicit_0, params) == 0

    # 显式正数仍可限制;任务属性可覆盖。
    agent_explicit_10 = SimpleNamespace(config=AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=10))
    assert _effective_max_tool_rounds(agent_explicit_10, params) == 10
    params_override = SimpleNamespace(task_attributes={"max_tool_rounds": 17})
    assert _effective_max_tool_rounds(agent, params_override) == 17


# LLM: 所有覆盖为明确 typed 字段，原守卫与限流 gate 的数值断言不变。
# 函数用途: 检查实际注册表消费者使用传入快照而非另一份默认。
def test_registry_runtime_gate_policy_uses_passed_runtime_policy():
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy
    from agent_py_agent.agent.tooling.action_policy import _tool_guardrail_config
    from agent_py_agent.agent.tooling.registry_rate_limit_policy import tool_rate_limit_policy

    policy = RuntimeGuardPolicy(repeat_fail_threshold=13, readonly_no_progress_threshold=4,
                               terminal_block_enabled=True, tool_rate_max_calls=14, tool_rate_window_seconds=15,
                               tool_circuit_failure_threshold=16, tool_circuit_backoff_seconds=(1, 3, 5),
                               tool_rate_max_records=17)

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


# LLM: 未授权动作属性只能收紧硬门，不能以 0/false/大阈值关闭部署保护；成功重复软提醒仍保原覆盖规则。
# 函数用途: 通过真实 guard reader 验证任务属性不能放大硬限制。
def test_config_task_attributes_cannot_weaken_host_hard_guards():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_guard.call_guardrail_config import (
        hard_failure_halt_enabled,
        hard_failure_halt_threshold,
        repeated_failure_halt_threshold,
        terminal_block_enabled,
    )
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy

    params = SimpleNamespace(runtime_guard_policy=RuntimeGuardPolicy(terminal_block_enabled=True,
                             hard_failure_halt_enabled=True, hard_failure_halt_threshold=4,
                             repeated_failure_halt_threshold=6),
                             task_attributes={"terminal_block_enabled": False, "hard_failure_halt_enabled": False,
                                              "hard_failure_halt_threshold": 100, "repeated_failure_halt_threshold": 0})
    assert terminal_block_enabled(params) is True
    assert hard_failure_halt_enabled(params) is True
    assert hard_failure_halt_threshold(params) == 4
    assert repeated_failure_halt_threshold(params) == 6
    params.task_attributes = {"hard_failure_halt_threshold": 2, "repeated_failure_halt_threshold": 3}
    assert hard_failure_halt_threshold(params) == 2
    assert repeated_failure_halt_threshold(params) == 3
# LLM: task 的恢复额度只能收紧宿主正上限；显式零不能把宿主额度改成无限。
# 函数用途: 验证主代理自动恢复也遵守同一部署硬上限。
def test_config_auto_resume_request_cannot_weaken_host_limit():
    from types import SimpleNamespace

    from agent_py_agent.agent.contracts import main_agent_auto_resume as module
    from agent_py_agent.agent.settings.runtime_guard_config import RuntimeGuardPolicy

    # 宿主上限经冻结 typed 快照传入，不再替换模块内读取函数。
    host = RuntimeGuardPolicy(main_agent_auto_resume_attempt_limit=2)
    for requested, expected in ((0, 2), (5, 2), (1, 1), (None, 2)):
        request = SimpleNamespace(max_auto_recovery_attempts=requested)
        assert module.auto_resume_limit(request, policy=host) == expected
    assert RuntimeGuardPolicy().main_agent_auto_resume_attempt_limit == module.DEFAULT_AUTO_RECOVERY_ATTEMPT_COUNT
