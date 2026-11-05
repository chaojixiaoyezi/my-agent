"""压缩水位以实际共享容量约束；未知容量不升级为物理保证。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy
from agent_py_agent.tests.test_compact_output_policy import _responses_agent


@pytest.mark.parametrize("window,cap,expected", [(272000,300000,217600),
    (400000,300000,300000), (400000,0,320000)])
def test_default_272k_and_400k_triggers(monkeypatch, window, cap, expected):
    agent, _ = _responses_agent(monkeypatch, window=window,
                               memory_compact_auto_trigger_max_tokens=cap)
    policy = runtime_compact_policy(agent)
    assert policy.trigger_tokens == expected
    assert policy.recovery_target_tokens < policy.trigger_tokens


def test_absolute_zero_and_forced_percent_still_obey_capacity(monkeypatch):
    agent, _ = _responses_agent(monkeypatch, memory_compact_trigger_percent=95,
        memory_compact_auto_trigger_max_tokens=0, force_compact_trigger_percent=True)
    policy = runtime_compact_policy(agent)
    assert policy.trigger_tokens == 249472
    assert policy.trigger_capped
    assert policy.trigger_limit_reason == "capacity"
    assert policy.compact_output_reserve_tokens == 16384
    assert policy.force_locked
    assert policy.recovery_target_tokens == policy.trigger_tokens * 60 // 95
    agent.config = replace(agent.config, memory_compact_auto_trigger_max_tokens=200000)
    smaller = runtime_compact_policy(agent)
    assert smaller.trigger_tokens == 200000
    assert smaller.trigger_limit_reason == "absolute_cap"


def _request_at_tokens(request, target):
    from agent_py_agent.agent.conversation.compact_request_budget import compact_request_tokens

    low, high = 0, target * 4
    while low < high:
        mid = (low + high + 1) // 2
        candidate = replace(request, messages=[{"role": "user", "content": "x" * mid}])
        if compact_request_tokens(candidate) <= target:
            low = mid
        else:
            high = mid - 1
    candidate = replace(request, messages=[{"role": "user", "content": "x" * low}])
    assert compact_request_tokens(candidate) == target
    return candidate


def test_suffix_allowance_used_once_and_real_suffix_measured(monkeypatch):
    from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
    from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource
    from agent_py_agent.agent.conversation.compact_request_budget import (
        compact_cache_surface_budget,
        generate_bounded_compact_response,
    )
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt
    from agent_py_agent.tests.test_compact_output_policy import _summary_request

    agent, sent = _responses_agent(monkeypatch, memory_compact_trigger_percent=95, memory_compact_auto_trigger_max_tokens=0)
    policy = runtime_compact_policy(agent)
    ceiling = compact_cache_surface_budget(agent)
    assert ceiling - policy.trigger_tokens == 4096
    request = replace(_summary_request(agent), prompt=CacheStructuredPrompt("稳定前缀", "x" * 18000))
    request = _request_at_tokens(request, ceiling)
    source = CompactMessageSource(lambda: iter(request.messages), preserve_prefix=True)
    response = generate_bounded_compact_response(replace(request, messages=None), message_source=source)
    assert response.request_token_budget.input_tokens == ceiling
    assert len(sent) == 1 and response.request_token_budget.output_tokens == 16384
    too_large = replace(request, prompt=CacheStructuredPrompt("稳定前缀", "x" * 36000),
                        messages=None, token_budget=response.request_token_budget)
    with pytest.raises(ConversationCompactError) as error:
        generate_bounded_compact_response(too_large, message_source=source)
    assert error.value.code == "COMPACT_PREFIX_TOO_LARGE" and len(sent) == 1


def test_finalization_ui_recovery_and_main_preflight_share_effective_trigger(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import finalization_compact_auto
    from agent_py_agent.agent.conversation.compact import (
        inspect_conversation_context,
        render_conversation_context_usage,
    )
    from agent_py_agent.tests.test_compact_capacity_facts import _transcript_case
    from agent_py_agent.tests.test_compact_trigger_cap import _finalize_context_for_continuation
    from agent_py_agent.tests.test_request_token_budget import _main_response

    agent, store, thread = _transcript_case(tmp_path, monkeypatch, turns=2)
    controlled, sent = _responses_agent(monkeypatch, memory_compact_trigger_percent=95,
        memory_compact_auto_trigger_max_tokens=0, force_compact_trigger_percent=True, request_timeout=0)
    agent.backend = controlled.backend
    agent.config = replace(controlled.config, my_agent_home=agent.config.my_agent_home)
    policy = runtime_compact_policy(agent)
    ctx = replace(_finalize_context_for_continuation(tool_rounds=0, executed_tools=[]), compact_auto_continue_depth=0)
    captured = []

    def cycle(root, options):
        captured.append(options)
        raise RuntimeError("capture effective trigger")

    monkeypatch.setattr(finalization_compact_auto, "run_memory_compact_auto_cycle", cycle)
    with pytest.raises(RuntimeError, match="capture effective trigger"):
        finalization_compact_auto.compact_auto_cycle_fields(agent, ctx, {"active": 249472, "turn": 249472})
    assert captured[0].trigger_tokens == policy.trigger_tokens == 249472
    assert policy.recovery_target_tokens == policy.trigger_tokens * 60 // 95
    assert policy.recent_tail_tokens == 20000
    usage = inspect_conversation_context(agent, store, thread)
    assert usage.trigger_tokens == policy.trigger_tokens and usage.trigger_limit_reason == "capacity"
    rendered = render_conversation_context_usage(usage, model_name="fixture")
    assert "容量" in rendered and "绝对上限" not in rendered
    _, params, _ = _main_response(monkeypatch, agent, policy.trigger_tokens - 1)
    count = len(sent)
    pressured, _, _ = _main_response(monkeypatch, agent, policy.trigger_tokens, params=replace(params, save=True))
    assert pressured.runtime_status == "context_overflow" and len(sent) == count
    assert "compact_threshold=249472" in pressured.text


def test_overshoot_rejects_before_io_without_history_mutation(monkeypatch):
    from copy import deepcopy

    from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
    from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource
    from agent_py_agent.agent.conversation.compact_request_budget import (
        compact_cache_surface_budget,
        generate_bounded_compact_response,
    )
    from agent_py_agent.tests.test_compact_output_policy import _summary_request

    agent, sent = _responses_agent(monkeypatch)
    request = _request_at_tokens(_summary_request(agent), compact_cache_surface_budget(agent) + 10000)
    history = deepcopy(request.messages)
    source = CompactMessageSource(lambda: iter(request.messages), preserve_prefix=True)
    with pytest.raises(ConversationCompactError) as error:
        generate_bounded_compact_response(replace(request, messages=None), message_source=source)
    assert error.value.code == "COMPACT_PREFIX_TOO_LARGE"
    assert not sent and request.messages == history


def test_post_compact_candidate_uses_dynamic_main_budget(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.compact import (
        ConversationCompactOptions,
        prepare_conversation_context,
    )
    from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
    from agent_py_agent.tests.test_compact_capacity_facts import _transcript_case
    from agent_py_agent.tests.test_request_token_budget import _main_response

    agent, store, thread = _transcript_case(tmp_path, monkeypatch, turns=7)
    controlled, sent = _responses_agent(monkeypatch, request_timeout=0)
    agent.backend = controlled.backend
    agent.config = replace(controlled.config, my_agent_home=agent.config.my_agent_home)
    result = prepare_conversation_context(agent, store, thread, options=ConversationCompactOptions(force=True,
        request_projector=lambda view: ConversationCompactProjection(200000 if view.is_candidate else 260000, object())))
    assert result.compacted and result.projected_tokens == 200000
    assert result.thread.compact_generation == 1 and len(sent) == 1
    # 200K大于旧W-128K门；真实下一dispatcher仍应能按剩余容量发送。
    response, _, _ = _main_response(monkeypatch, agent, result.projected_tokens)
    assert sent[-1]["max_output_tokens"] == response.request_token_budget.output_tokens == 69952
    assert len(sent) == 2 and agent.backend.max_tokens == 68000


def test_planner_import_order_has_no_cycle():
    import os
    import subprocess
    import sys
    from pathlib import Path

    modules = ["agent_py_agent.agent.backends.request_budget", "agent_py_agent.agent.agent_core.model.call_runtime",
               "agent_py_agent.agent.agent_core.model.context_pressure", "agent_py_agent.agent.agent_core.runtime.context_compactor"]
    root = Path(__file__).resolve().parents[2]
    script = "import importlib,sys; [importlib.import_module(name) for name in sys.argv[1:]]"
    for order in [modules, list(reversed(modules)), modules[2:] + modules[:2]]:
        run = subprocess.run([sys.executable, "-c", script, *order], cwd=root,
            env=os.environ | {"PYTHONPATH": f"{root}:{root / 'agent_py_agent'}", "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stderr


def test_unknown_window_keeps_conservative_reserve_without_shared_capacity_claim(monkeypatch):
    from agent_py_agent.agent.agent_core.model.call_runtime import request_budget_shared_window
    from agent_py_agent.agent.agent_core.runtime import context_compactor
    from agent_py_agent.agent.conversation.compact_request_budget import (
        compact_cache_surface_budget,
        compact_summary_budget,
    )
    from agent_py_agent.tests.test_request_token_budget import _main_response

    agent, sent = _responses_agent(monkeypatch, window=0, memory_compact_trigger_percent=95,
        memory_compact_auto_trigger_max_tokens=0, force_compact_trigger_percent=True, request_timeout=0)
    agent.backend._provider_context_window_cache = 272000
    agent.backend.model_metadata = {"input_token_limit": 272000}
    assert not request_budget_shared_window(agent)
    policy = runtime_compact_policy(agent)
    assert policy.trigger_tokens == 258400 and policy.trigger_limit_reason == "percent"
    assert compact_cache_surface_budget(agent) == 255616
    assert compact_summary_budget(agent) == 201216
    result, _, _ = _main_response(monkeypatch, agent, 50000)
    assert not result.request_token_budget.shared_window
    assert sent[0]["max_output_tokens"] == agent.backend.max_tokens
    monkeypatch.setattr(context_compactor, "resolve_model_context_window_tokens", lambda agent: 0)
    unknown = runtime_compact_policy(agent)
    assert unknown.trigger_tokens == unknown.recovery_target_tokens == 0


# LLM: 元数据窗口不提供共享容量证明；同样的非法共享几何仍必须失败。
# 函数用途: 防止普通状态查看因旧后端的输出估算而被共享窗口校验阻断。
def test_nonshared_trigger_does_not_enforce_shared_output_geometry():
    from agent_py_agent.agent.backends.request_budget import (
        RequestBudgetExceeded,
        compact_trigger_limit,
    )

    facts = dict(window=10000, percent=80, absolute_cap=300000, compact_reserve=16000)
    assert compact_trigger_limit(**facts, shared_window=False) == 8000
    with pytest.raises(RequestBudgetExceeded) as caught:
        compact_trigger_limit(**facts, shared_window=True)
    assert caught.value.reason == "invalid_geometry"
