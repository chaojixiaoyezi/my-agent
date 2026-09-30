"""自动压缩触发线的绝对上限 memory_compact_auto_trigger_max_tokens（2026-09-29，38 调查的方案 A）。

起因：模型档案把窗口填成 1,000,000，触发线走默认 90% 就是 90 万；长期后台线程在 30 万–80 万之间跑好几个小时才压，
四个线程并发顶住了服务商的每分钟 token 上限。百分比钳在 [50,100]，1M 窗口下最低只到 50 万，所以加一个绝对上限。

钉住：
1. 上限为 0（默认）或非法值时，策略与之前完全一样；
2. 上限严格低于“窗口 × 百分比”时触发线 = 上限，近期尾部与 recovery 都从封顶后的触发线推出：recovery 按
   触发线 × recovery% ÷ 触发% 等比推导（30 万时是 20 万），且不超过“触发线 − 近期尾部”；上限正好等于“窗口 × 百分比”
   时不算封顶，一切按原百分比口径；
3. 配置解析：合法值原样生效，负数、非整数回到 0 并告警；
4. 模型请求前预检在上限处触发（前台同一入口），后台定时回合的预检也在上限处先压缩；
5. finalization 自动压缩周期：封顶时按 token 触发线判断，不封顶时仍按百分比（原行为）；
6. /context 在封顶时说清楚是绝对上限；
7. 这个键在自助修改白名单里：接受的值原样生效，回执与百分比项同样写“重启 Gateway 才生效”。
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import finalization_compact_auto
from agent_py_agent.agent.agent_core.model.context_pressure import (
    preflight_context_pressure_response,
)
from agent_py_agent.agent.agent_core.runtime.context_compactor import (
    compact_recovery_target_tokens,
    compact_trigger_max_tokens,
    runtime_compact_policy,
)
from agent_py_agent.agent.conversation import background_execution
from agent_py_agent.agent.conversation.compact import (
    ConversationContextUsage,
    inspect_conversation_context,
    render_conversation_context_usage,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
from agent_py_agent.agent.memory_archive.compact_auto import (
    MemoryCompactAutoCycleOptions,
    run_memory_compact_auto_cycle,
)
from agent_py_agent.agent.memory_archive.compact_suggest import (
    MemoryCompactSuggestOptions,
    build_memory_compact_suggestion,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.memory import normalize_memory_settings
from agent_py_agent.agent.settings.user_config_capability import TUNABLE_KEYS, set_tunable_value
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot
from agent_py_agent.tests.memory_compact_support import write_compact_fixture
from agent_py_agent.tests.test_background_compact_recovery import _background
from agent_py_agent.tests.test_memory_runtime_compact_auto_continuation import (
    _finalize_context_for_continuation,
)
from agent_py_agent.tests.test_subagent_compact_recovery import _http

WINDOW = 1_000_000


# 函数用途: 造一个只带窗口与三项压缩配置的轻量 agent，给 runtime_compact_policy 用。
def _agent(cap, *, percent=90, recovery=60):
    return SimpleNamespace(
        config=SimpleNamespace(
            memory_compact_auto_trigger_percent=percent,
            memory_compact_recovery_target_percent=recovery,
            memory_compact_auto_trigger_max_tokens=cap,
        ),
        backend=SimpleNamespace(context_window_tokens=WINDOW),
    )


# 函数用途: 取一次策略里与触发线相关的全部数值，便于整体比较。
def _limits(policy):
    return (policy.context_window_tokens, policy.trigger_percent, policy.trigger_tokens,
            policy.recovery_target_tokens, policy.recent_tail_tokens)


def test_zero_or_invalid_cap_keeps_the_policy_unchanged():
    baseline = runtime_compact_policy(SimpleNamespace(
        config=SimpleNamespace(memory_compact_auto_trigger_percent=90, memory_compact_recovery_target_percent=60),
        backend=SimpleNamespace(context_window_tokens=WINDOW),
    ))
    assert _limits(baseline) == (WINDOW, 90, 900_000, 600_000, 20_000)
    for cap in (0, None, "abc", -5, True, "", 2.5e-1):
        policy = runtime_compact_policy(_agent(cap))
        assert _limits(policy) == _limits(baseline), cap
        assert policy.trigger_capped is False
    assert [compact_trigger_max_tokens(value) for value in (0, None, "abc", -5, True, "300000", 250_000)] == [
        0, 0, 0, 0, 0, 300_000, 250_000]


# 每组是 (上限, 触发百分比, 恢复百分比, 近期尾部, 恢复目标)。
@pytest.mark.parametrize("case", [
    (300_000, 90, 60, 20_000, 200_000),
    (100_000, 90, 60, 10_000, 66_666),
    (300_000, 80, 50, 20_000, 187_500),
    # 浮点比值（41 ÷ 80）会算成 51,249；整数先乘后除才是 51,250。
    (100_000, 80, 41, 10_000, 51_250),
    # 等比结果（48 万）超过“触发线 − 近期尾部”时取后者。
    (300_000, 50, 80, 20_000, 280_000),
])
def test_cap_below_the_percent_trigger_caps_trigger_tail_and_recovery(case):
    cap, percent, recovery_percent, tail, recovery = case
    policy = runtime_compact_policy(_agent(cap, percent=percent, recovery=recovery_percent))

    assert policy.trigger_tokens == cap and policy.trigger_capped is True
    assert policy.trigger_percent == percent, "百分比仍是配置值，只是触发线被上限压下来"
    assert policy.recent_tail_tokens == tail
    assert policy.recovery_target_tokens == recovery
    assert policy.recovery_target_tokens <= policy.trigger_tokens - tail, "recovery 必须跟随封顶后的触发线"


def test_uncapped_recovery_keeps_the_window_formula():
    for cap in (0, 900_000, 950_000):
        policy = runtime_compact_policy(_agent(cap))
        assert policy.recovery_target_tokens == compact_recovery_target_tokens(WINDOW, 900_000, 20_000, 60) == 600_000


@pytest.mark.parametrize("cap", [950_000, 900_000])
def test_cap_at_or_above_the_percent_trigger_changes_nothing(cap):
    policy = runtime_compact_policy(_agent(cap))

    assert _limits(policy) == (WINDOW, 90, 900_000, 600_000, 20_000)
    assert policy.trigger_capped is False and policy.trigger_max_tokens == cap


def test_config_parsing_accepts_positive_values_and_falls_back_to_zero():
    assert AgentConfig().memory_compact_auto_trigger_max_tokens == 0
    settings, warnings = normalize_memory_settings({})
    assert settings.memory_compact_auto_trigger_max_tokens == 0 and warnings == []
    settings, warnings = normalize_memory_settings({"memory_compact_auto_trigger_max_tokens": "300000"})
    assert settings.memory_compact_auto_trigger_max_tokens == 300_000 and warnings == []
    for raw in ("abc", -1, True):
        settings, warnings = normalize_memory_settings({"memory_compact_auto_trigger_max_tokens": raw})
        assert settings.memory_compact_auto_trigger_max_tokens == 0, raw
        assert [item.field_name for item in warnings] == ["memory_compact_auto_trigger_max_tokens"], raw


@pytest.mark.parametrize("cap", [0, 300_000])
def test_request_preflight_triggers_at_the_cap(monkeypatch, cap):
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True, model_context_window_tokens=WINDOW,
            memory_compact_auto_trigger_percent=90, memory_compact_auto_trigger_max_tokens=cap,
        ),
        backend=SimpleNamespace(context_window_tokens=WINDOW, name="fake"),
    )
    params = SimpleNamespace(
        context_scope="default", live_archive_state={},
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
    )
    request = SimpleNamespace(agent=agent, params=params, prompt="系统上下文", tool_rounds=3)
    estimate = {"tokens": 299_999}
    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.model.context_pressure.estimate_tokens", lambda _prompt: estimate["tokens"],
    )
    assert preflight_context_pressure_response(request) is None

    estimate["tokens"] = 300_000
    response = preflight_context_pressure_response(request)
    if cap:
        assert response is not None and response.runtime_status == "context_overflow"
        assert "compact_threshold=300000" in response.text
    else:
        assert response is None, "不封顶时仍是 90 万触发"


# 后台定时回合：窗口 1M、默认 90%，只有上限把触发线压到 17,550（与既有后台用例的触发线相同）时，
# 首个后台请求前先压缩；不封顶的对照组直接发业务请求、不压缩。
@pytest.mark.parametrize("cap", [0, 17_550])
def test_background_scheduled_turn_precheck_compacts_at_the_cap(tmp_path, monkeypatch, cap):
    agent, store, thread, request, execution, sink = _background(
        tmp_path, backend="openai_compatible", detached=False,
    )
    assert request.reason == "scheduled_progress_report"
    agent.config.model_context_window_tokens = WINDOW
    agent.config.memory_compact_auto_trigger_max_tokens = cap
    agent.config.max_tokens = agent.backend.max_tokens = 1_024
    for role in ("user", "assistant"):
        store.messages.append({
            "thread_id": thread.thread_id, "role": role,
            "content": "补充的构建、测试与发布记录" * 250,
            "metadata": {"conversation_request_id": f"completed-extra-{role}", "task_id": request.task_id},
            "now": 14.0 if role == "user" else 15.0,
        })
    generations = []
    _http(monkeypatch, backend="openai_compatible",
          on_business=lambda _wire, _number: generations.append(
              store.threads.require(thread.thread_id).compact_generation))

    result = background_execution.run_background_turn_with_compact(
        execution, thread, request, user_prompt="继续核对本轮资料", continuation_injection=[],
        proactive_delivery_available=False, activity_sink=sink,
    )

    assert result.runtime_status != "context_overflow"
    final = store.threads.require(thread.thread_id).compact_generation
    assert final == (1 if cap else 0)
    assert generations and generations[-1] == final, "业务请求在压缩提交之后才发出"


# 函数用途: 生成一次 compact 建议，只改 token 事实与触发线。
def _suggest(tmp_path, *, current, trigger_tokens):
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    return build_memory_compact_suggestion(root, MemoryCompactSuggestOptions(
        current_tokens=current, max_context_tokens=WINDOW, trigger_percent=90, trigger_tokens=trigger_tokens,
        plan_options=MemoryCompactPlanOptions(session_id="session-cap", request_id="request-cap"),
    ))


def test_finalization_suggestion_uses_token_trigger_only_when_given(tmp_path):
    capped = _suggest(tmp_path / "a", current=300_000, trigger_tokens=300_000)
    assert capped["status"] == "ready_to_compact"
    assert capped["message"] == "context usage is 30%; reached auto compact trigger 30%."
    assert _suggest(tmp_path / "b", current=299_999, trigger_tokens=300_000)["status"] == "ok"
    assert _suggest(tmp_path / "c", current=300_000, trigger_tokens=0)["status"] == "ok", "不传 token 触发线时仍按 90%"
    assert _suggest(tmp_path / "d", current=900_000, trigger_tokens=0)["status"] == "ready_to_compact"


# 自动压缩周期把 token 触发线原样交给建议：同样 30 万，传了就到线（等确认），不传仍按 90% 没到线。
def test_auto_cycle_passes_the_token_trigger_to_the_suggestion(tmp_path):
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    plan = MemoryCompactPlanOptions(session_id="session-cap", request_id="request-cap")
    capped = run_memory_compact_auto_cycle(root, MemoryCompactAutoCycleOptions(
        current_tokens=300_000, max_context_tokens=WINDOW, plan_options=plan, trigger_tokens=300_000))
    plain = run_memory_compact_auto_cycle(root, MemoryCompactAutoCycleOptions(
        current_tokens=300_000, max_context_tokens=WINDOW, plan_options=plan))

    assert capped["status"] == "needs_user_confirmation"
    assert plain["status"] == "skipped_below_threshold"


# 函数用途: 跑一次 finalization 自动压缩字段计算，只截下交给压缩周期的输入，不真正压缩。
def _cycle_options(tmp_path, monkeypatch, cap):
    agent = SimpleAgent(AgentConfig(
        model_backend="echo", my_agent_home=str(tmp_path / "home"), model_context_window_tokens=WINDOW,
        memory_compact_auto_trigger_max_tokens=cap,
    ), tmp_path)
    ctx = replace(_finalize_context_for_continuation(tool_rounds=0, executed_tools=[]), compact_auto_continue_depth=0)
    seen = []

    # 函数用途: 记下压缩周期收到的输入后中止，证明 finalization 把哪条触发线交了出去。
    def capture(_root, options):
        seen.append(options)
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(finalization_compact_auto, "run_memory_compact_auto_cycle", capture)
    with pytest.raises(RuntimeError, match="stop after capture"):
        finalization_compact_auto.compact_auto_cycle_fields(agent, ctx, {"turn": 10, "active": 10})
    [options] = seen
    return options


@pytest.mark.parametrize(("cap", "expected"), [(0, 0), (950_000, 0), (900_000, 0), (300_000, 300_000)])
def test_finalization_cycle_receives_the_token_trigger_only_when_capped(tmp_path, monkeypatch, cap, expected):
    options = _cycle_options(tmp_path, monkeypatch, cap)

    assert options.trigger_percent == 90 and options.max_context_tokens == WINDOW
    assert options.trigger_tokens == expected


def test_context_view_says_the_trigger_is_capped():
    usage = ConversationContextUsage(
        projected_tokens=120_000, context_window_tokens=WINDOW, trigger_percent=90, trigger_tokens=300_000,
        compact_generation=0, compact_source_messages=0, compact_source_tool_pairs=0, terminal_tool_fold_turns=0,
        terminal_tool_fold_calls=0, pending_messages=3, has_summary=False, trigger_capped=True,
    )
    capped = render_conversation_context_usage(usage, model_name="m")
    plain = render_conversation_context_usage(replace(usage, trigger_capped=False), model_name="m")

    assert "自动 compact：开启，300,000 tokens 触发（绝对上限封顶，比 90% 更早）；距触发线约 180,000 tokens" in capped
    assert "自动 compact：开启，90% （300,000 tokens）触发；" in plain


@pytest.mark.parametrize(("cap", "trigger", "capped"), [
    (0, 900_000, False), (900_000, 900_000, False), (300_000, 300_000, True),
])
def test_context_inspection_reports_the_capped_trigger(tmp_path, cap, trigger, capped):
    agent = SimpleAgent(AgentConfig(
        model_backend="echo", my_agent_home=str(tmp_path / "home"), model_context_window_tokens=WINDOW,
        memory_compact_auto_trigger_max_tokens=cap,
    ), tmp_path)

    usage = inspect_conversation_context(agent, agent.conversation_store, None, current_prompt="看一下上下文")

    assert (usage.trigger_tokens, usage.trigger_capped) == (trigger, capped)


def test_self_service_cap_is_accepted_exactly_when_it_takes_effect():
    """自助修改接受的上限必须原样生效：与配置解析、运行时规范化三处一致；0 表示不封顶。"""
    spec = TUNABLE_KEYS["memory_compact_auto_trigger_max_tokens"]
    for value in (-5, -1, 0, 1, 250_000, 300_000, 10_000_000, 50_000_000):
        accepted = spec.validate(str(value))[0]
        parsed = normalize_memory_settings({"memory_compact_auto_trigger_max_tokens": value})[0]
        assert accepted == (compact_trigger_max_tokens(value) == value) == (
            parsed.memory_compact_auto_trigger_max_tokens == value), value
    for raw in ("abc", "1.5", ""):
        assert spec.validate(raw)[0] is False, raw


def test_self_service_cap_receipt_says_restart_like_the_percent(tmp_path):
    user = tmp_path / "desktop.yaml"
    user.write_text("my_agent_home: /tmp/home\n", encoding="utf-8")

    cap = set_tunable_value("memory_compact_auto_trigger_max_tokens", "300000", user_path=user)
    percent = set_tunable_value("memory_compact_auto_trigger_percent", "80", user_path=user)

    assert cap["ok"] is True and cap["saved"] == "300000" and cap["written_value_matches"] is True
    assert cap["effect_text"] == percent["effect_text"] and "重启 Gateway 才生效" in cap["effect_text"]
    assert set_tunable_value("memory_compact_auto_trigger_max_tokens", "-1", user_path=user)["ok"] is False
