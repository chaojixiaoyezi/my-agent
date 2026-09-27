"""参数减量第 2 批（2026-09-27）：11 组重复参数各合并成一个旋钮，钉住合并后的语义。

锁定：被吸收的旧键写在 YAML 里只告警并忽略，不留兼容别名；每个保留旋钮的“空 / 0 / 数字”含义；
随包 YAML 与 AgentConfig 默认值一致。runner 重跑次数、并发、>8 的工具并行、单代理预算另有专门测试文件。
"""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.settings.config import AgentConfig, load_config, normalize_agent_config

_PACKAGED = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
_ABSORBED = {
    "runner_auto_concurrency": "4",
    "background_claim_heartbeat_interval_seconds": "10",
    "max_tool_calls_per_round": "3",
    "tool_agent_budget_window_seconds": "60",
    "tool_artifact_read_budget_window_seconds": "60",
    "memory_hook_archive_level": "1",
    "memory_rule_routing_enabled": "false",
    "memory_resume_auto_context_enabled": "true",
    "model_context_window_explicit": "true",
    "model_temperature_explicit": "true",
    "runner_failure_policy": '"off"',
}
_SURVIVORS = (
    "runner_concurrency", "background_claim_ttl_seconds", "max_parallel_tool_calls", "tool_agent_budget_max_calls",
    "tool_artifact_read_budget_max_chars", "memory_archive_level", "memory_rule_routing_mode",
    "memory_resume_auto_context_mode", "model_context_window_tokens", "temperature", "runner_failure_retry_limit",
)


def test_absorbed_keys_are_gone_and_only_warn_when_left_in_yaml(tmp_path):
    names = {item.name for item in fields(AgentConfig)}
    assert not names & set(_ABSORBED)
    path = tmp_path / "agent_config.yaml"
    path.write_text("".join(f"{key}: {value}\n" for key, value in _ABSORBED.items()), encoding="utf-8")

    config = load_config(path)

    for key in _ABSORBED:
        assert f"unknown config key: {key!r}; ignored" in config.config_warnings
        assert not hasattr(config, key)
    # 旧键不会被自动转成新值：保留旋钮仍是各自默认。
    for key in _SURVIVORS:
        assert getattr(config, key) == getattr(AgentConfig(), key), key


def test_packaged_yaml_matches_dataclass_defaults_for_every_survivor():
    config = load_config(_PACKAGED)
    defaults = AgentConfig()
    for key in _SURVIVORS:
        assert getattr(config, key) == getattr(defaults, key), key
        assert not [warning for warning in config.config_warnings if key in warning], key
    assert (defaults.memory_resume_auto_context_mode, defaults.model_context_window_tokens, defaults.temperature) == (
        "off", None, None)


def test_blank_numbers_mean_default_without_warnings():
    """YAML 里 `key:` 留空读成 []：整数旋钮与温度都按“没填”处理，不再每次启动告警。"""
    normalized, warnings = normalize_agent_config({
        "max_parallel_tool_calls": [], "model_context_window_tokens": [], "temperature": [],
        "tool_agent_budget_max_calls": "",
    })
    assert warnings == []
    assert normalized["max_parallel_tool_calls"] is None
    assert normalized["model_context_window_tokens"] is None
    assert normalized["temperature"] is None
    assert normalized["tool_agent_budget_max_calls"] is None


@pytest.mark.parametrize("value,expected", [(0, 0), ("3", 3), (12, 12)])
def test_max_parallel_tool_calls_accepts_zero_and_small_values(value, expected):
    """原规范化把最小值卡在 8：0（不限制）和 1-7 写了也会被退回默认。"""
    normalized, warnings = normalize_agent_config({"max_parallel_tool_calls": value})
    assert warnings == [] and normalized["max_parallel_tool_calls"] == expected


def test_runner_concurrency_zero_through_config_means_whole_batch():
    from agent_py_agent.agent.agent_core.runner.timeout_policy import resolve_runner_config

    _timeout, concurrency, _rate = resolve_runner_config(AgentConfig(runner_concurrency="0"), 20)
    assert concurrency == 20
    _timeout, concurrency, _rate = resolve_runner_config(AgentConfig(), 20)
    assert concurrency == 8


def test_runner_failure_retry_limit_rejects_negative_numbers():
    normalized, warnings = normalize_agent_config({"runner_failure_retry_limit": -1})
    assert normalized["runner_failure_retry_limit"] == 1 and warnings


@pytest.mark.parametrize("ttl,expected", [(90, 30.0), (300, 100.0), (30, 10.0)])
def test_claim_heartbeat_is_always_derived_from_ttl(ttl, expected):
    from agent_py_agent.agent.conversation.run_claim import claim_heartbeat_interval_seconds

    assert claim_heartbeat_interval_seconds(ttl_seconds=ttl) == expected


def test_runner_session_heartbeat_is_a_fixed_constant():
    from agent_py_agent.agent.agent_core.runner import worker

    assert worker._RUNNER_SESSION_HEARTBEAT_SECONDS == 5.0
    assert not hasattr(worker, "_runner_session_heartbeat_interval")


def test_artifact_read_budget_window_is_fixed_and_zero_chars_means_unlimited(tmp_path):
    from agent_py_agent.agent.tooling.artifact import (
        ArtifactReadBudget,
        ArtifactReadBudgetRequest,
        ReadArtifactTool,
    )

    tool = ReadArtifactTool(tmp_path, artifact_read_budget_max_chars=10)
    assert (tool.read_budget.window_seconds, tool.read_budget.max_chars) == (600, 10)
    assert ReadArtifactTool(tmp_path).read_budget.max_chars == AgentConfig().tool_artifact_read_budget_max_chars
    unlimited = ArtifactReadBudget(window_seconds=600, max_chars=0)
    assert unlimited.reserve(ArtifactReadBudgetRequest(run_id="run-1", requested_chars=10**9)) == (None, "")


def test_subagent_recovery_snapshot_uses_the_single_archive_level():
    from agent_py_agent.agent.agent_core.subagent_mixin import _recovery_snapshot_input

    agent = SimpleNamespace(session_id="s-1", config=SimpleNamespace(agent_name="a", memory_archive_level=1))
    snapshot = SimpleNamespace(user_prompt="u", response_text="r", backend="echo", run_id="run-1",
                               status="DONE", error_code="", tool_calls=[])
    assert _recovery_snapshot_input(agent, snapshot, []).archive_level == 1


def test_rule_routing_off_is_the_only_off_switch(tmp_path):
    """原 memory_rule_routing_enabled=false 对应 mode=off；决策点召回在 off 下不再报“off 不是合法模式”的 finding。"""
    from agent_py_agent.agent.memory_push import (
        _route_formal_lessons,
        push_relevant_memories_report,
    )
    from agent_py_agent.tests._memory_push_v2_harness import formal_memory_agent

    agent = formal_memory_agent(tmp_path)
    agent.config.memory_rule_routing_mode = "off"

    assert _route_formal_lessons(agent, "规划 失败", 3).enabled is False
    assert push_relevant_memories_report(agent, "planning", {"goal": "规划失败后的处理"}) == ([], [])
    agent.config.memory_rule_routing_mode = "soft"
    assert _route_formal_lessons(agent, "规划 失败", 3).enabled is True


def test_prompt_routing_off_skips_the_formal_index():
    from agent_py_agent.agent.agent_core.runtime.loop_support import (
        _routed_memory_context_for_request,
    )

    agent = SimpleNamespace(config=SimpleNamespace(memory_rule_routing_mode="off", memory_rule_auto_read_limit=3))
    context = _routed_memory_context_for_request(agent, SimpleNamespace(user_prompt="规划"), skip_formal_recall=False)
    assert context.enabled is False and context.findings == []


def test_resume_mode_default_off_and_explicit_enable_forces_always(monkeypatch):
    from agent_py_agent.agent.memory_archive import resume_context

    monkeypatch.setattr(resume_context, "_build_resume_context",
                        lambda _agent, _prompt: resume_context.ResumeContextResult(reason="built"))

    def reason(mode, prompt, enabled=None):
        agent = SimpleNamespace(config=SimpleNamespace(memory_resume_auto_context_mode=mode))
        return resume_context.build_auto_resume_context(agent, prompt, enabled=enabled).reason

    assert AgentConfig().memory_resume_auto_context_mode == "off"
    assert reason("off", "继续 subagent-abc") == "off"
    assert reason("off", "继续", enabled=True) == "built"
    assert reason("always", "继续", enabled=False) == "disabled"
    assert reason("trigger", "继续上次的任务") == "no_trigger"
    assert reason("trigger", "继续 subagent-abc") == "built"
    assert reason("always", "你好") == "built"


def test_context_window_blank_uses_provider_then_128k_and_a_number_wins():
    from agent_py_agent.agent.agent_core.model.context_window import (
        resolve_model_context_window_tokens,
    )

    provider = SimpleNamespace(provider_context_window_tokens=200_000)
    blank = SimpleNamespace(model_context_window_tokens=None)

    assert resolve_model_context_window_tokens(SimpleNamespace(config=blank, backend=provider)) == 200_000
    assert resolve_model_context_window_tokens(SimpleNamespace(config=blank, backend=SimpleNamespace())) == 128_000
    explicit = SimpleNamespace(model_context_window_tokens=64_000)
    assert resolve_model_context_window_tokens(SimpleNamespace(config=explicit, backend=provider)) == 64_000


def test_output_reserve_needs_a_filled_window():
    from agent_py_agent.agent.agent_core.model.context_pressure import (
        _known_shared_window_output_reserve,
    )
    from agent_py_agent.agent.backends.base import BackendOptions
    from agent_py_agent.agent.backends.http import HttpBackend

    backend = HttpBackend(BackendOptions(api_base="https://example.invalid", api_key="k", model_name="m",
                                         context_window_tokens=10_000, max_tokens=2_000))
    blank = SimpleNamespace(config=SimpleNamespace(model_context_window_tokens=None), backend=backend)
    filled = SimpleNamespace(config=SimpleNamespace(model_context_window_tokens=10_000), backend=backend)
    assert _known_shared_window_output_reserve(blank) == 0
    assert _known_shared_window_output_reserve(filled) == 2_000


def test_default_model_row_shows_the_fallback_window_instead_of_none(tmp_path):
    """/model 列表的“默认”行直接显示窗口数字；部署配置留空时显示兜底 128000，而不是 None tokens。"""
    from agent_py_agent.agent.settings.model_profiles import (
        public_model_profiles,
        read_model_profiles,
    )

    config = AgentConfig(model_backend="openai_compatible", model_name="m", api_base="https://example.test/v1")
    data = read_model_profiles(tmp_path / "absent.json")
    assert public_model_profiles(data, config)["profiles"][0]["model_context_window_tokens"] == 128_000
    config.model_context_window_tokens = 64_000
    assert public_model_profiles(data, config)["profiles"][0]["model_context_window_tokens"] == 64_000


@pytest.mark.parametrize("raw,expected,warned", [
    (None, None, False), ("", None, False), ("0.7", "0.7", False), (0, "0.0", False), ("abc", None, True), (3, None, True),
])
def test_temperature_blank_means_not_sent(raw, expected, warned):
    normalized, warnings = normalize_agent_config({"temperature": raw})
    assert normalized["temperature"] == expected
    assert bool(warnings) is warned
