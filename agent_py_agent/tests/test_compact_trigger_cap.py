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
    DEFAULT_COMPACT_TRIGGER_MAX_TOKENS,
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


# LLM: 旧自动比例已随 a12fe40c5 退役：构造和replace都拒绝旧键；正式比例随复制原样继承，不改源对象也不残留私有来源字段。
# 函数用途: 核对连续修改正式比例及复制配置时各自独立，原配置保持不变。
@pytest.mark.parametrize("initial", [70, 85])
def test_r7_replace_keeps_canonical_trigger_without_mutating_source(initial):
    from dataclasses import asdict, fields

    with pytest.raises(TypeError):
        AgentConfig(memory_compact_auto_trigger_percent=initial)
    original = AgentConfig(memory_compact_trigger_percent=initial)
    with pytest.raises(TypeError):
        replace(original, memory_compact_auto_trigger_percent=90)
    changed = replace(original, memory_compact_trigger_percent=90)
    assert changed.memory_compact_trigger_percent == 90
    assert original.memory_compact_trigger_percent == initial
    changed.memory_compact_trigger_percent = 85
    copied = replace(changed, memory_compact_trigger_percent=75)
    assert copied.memory_compact_trigger_percent == 75
    assert changed.memory_compact_trigger_percent == 85
    assert not any(name.startswith("_compact_trigger_") for name in asdict(copied))
    assert not any(item.name.startswith("_compact_trigger_") for item in fields(copied))


# LLM: 退役旧键不能取得覆盖管理员正式字段的权限；replace直接拒绝，运行期同名普通属性既不联动、也不进入压缩策略或复制结果。
# 函数用途: 验证显式正式比例在旧名赋值和dataclasses.replace后均不被旧比例覆盖。
@pytest.mark.parametrize("operation", ["assign", "replace", "same-value"])
def test_r7_legacy_trigger_changes_preserve_explicit_new_trigger(operation):
    from agent_py_agent.agent.conversation.compact_threshold_policy import (
        resolve_thread_compact_policy,
    )

    config = AgentConfig(memory_compact_trigger_percent=75)
    if operation == "replace":
        with pytest.raises(TypeError):
            replace(config, memory_compact_auto_trigger_percent=85)
        result = config
    else:
        config.memory_compact_auto_trigger_percent = 75 if operation == "same-value" else 85
        result = replace(config)
        assert not hasattr(result, "memory_compact_auto_trigger_percent")
    assert result.memory_compact_trigger_percent == 75
    assert resolve_thread_compact_policy(SimpleNamespace(config=config))["effective_trigger_percent"] == 75


# LLM: 新旧比例并列不再按相等关系联动：同时给出旧键即被拒绝，只改正式比例的复制不影响源对象。
# 函数用途: 验证并列旧键被构造拒绝，正式比例复制更新只作用于副本。
def test_r7_replace_tracks_initially_equal_explicit_triggers():
    with pytest.raises(TypeError):
        AgentConfig(memory_compact_trigger_percent=70, memory_compact_auto_trigger_percent=70)
    config = AgentConfig(memory_compact_trigger_percent=70)
    changed = replace(config, memory_compact_trigger_percent=85)
    assert changed.memory_compact_trigger_percent == 85
    assert config.memory_compact_trigger_percent == 70


# LLM: 不存在私有复制上下文；旧名只是一条普通实例属性，不能把历史赋值当作正式比例仍受旧键控制的证据。
# 函数用途: 验证正式比例多次独立赋值后回到历史数值，普通复制或显式复制都不被旧名改写。
@pytest.mark.parametrize("overrides", [{}, {"memory_compact_trigger_percent": 70}, {"memory_compact_trigger_percent": 65}])
def test_r7_replace_does_not_revive_stale_trigger_link(overrides):
    config = AgentConfig(memory_compact_trigger_percent=75)
    config.memory_compact_auto_trigger_percent = 85
    config.memory_compact_trigger_percent = 70
    changed = replace(config, **overrides)
    assert changed.memory_compact_trigger_percent == overrides.get("memory_compact_trigger_percent", 70)
    assert config.memory_compact_trigger_percent == 70


# LLM: YAML同时配置显式默认正式比例80与退役旧比例70时，旧键按未知键告警忽略，后续克隆不能把80回退成70。
# 函数用途: 验证load_config加载并经dataclasses.replace克隆后显式80不被旧字段70覆盖。
def test_r7_yaml_explicit_default_canonical_trigger_survives_replace(tmp_path):
    from agent_py_agent.agent.settings.config import load_config

    cfg_path = tmp_path / "agent.yaml"
    cfg_path.write_text(
        "memory_compact_trigger_percent: 80\nmemory_compact_auto_trigger_percent: 70\n",
        encoding="utf-8",
    )
    loaded = load_config(cfg_path)
    assert loaded.memory_compact_trigger_percent == 80
    assert not hasattr(loaded, "memory_compact_auto_trigger_percent")
    assert "unknown config key: 'memory_compact_auto_trigger_percent'; ignored" in loaded.config_warnings
    cloned = replace(loaded, log_level="debug")
    assert cloned.memory_compact_trigger_percent == 80
    assert loaded.config_sources["memory_compact_trigger_percent"]["source"] == str(cfg_path.resolve())


# LLM: 正式比例范围外数值（如40）由四层策略夹取到50，之后改回范围内（如90）或经replace更新时，生效比例必须随之更新。
# 函数用途: 回归验证正式比例经范围夹取后再次修改不会停留在夹取值。
def test_r8_out_of_range_legacy_trigger_clamped_still_resyncs_on_update():
    from agent_py_agent.agent.conversation.compact_threshold_policy import (
        resolve_thread_compact_policy,
    )

    def effective(config):
        return resolve_thread_compact_policy(SimpleNamespace(config=config))["effective_trigger_percent"]

    cfg = AgentConfig()
    cfg.memory_compact_trigger_percent = 40
    assert effective(cfg) == 50
    cfg.memory_compact_trigger_percent = 90
    assert effective(cfg) == 90

    cfg_replace = AgentConfig(memory_compact_trigger_percent=40)
    assert effective(cfg_replace) == 50
    replaced = replace(cfg_replace, memory_compact_trigger_percent=90)
    assert effective(replaced) == 90


# LLM: replace不再接受退役旧键；任务/环境配置覆盖层同时写新旧比例时，旧键按未知运行配置键忽略，即使新比例等于80也以新键为准。
# 函数用途: 回归验证replace与runtime_config_task/runtime_config_env在新旧比例并列时始终保持新键权威。
@pytest.mark.parametrize("canonical_target", [70, 75, 80])
def test_r8_explicit_canonical_trigger_wins_over_co_specified_legacy_in_replace_and_overlays(
    tmp_path,
    canonical_target,
):
    from agent_py_agent.agent.settings.config import load_config
    from agent_py_agent.agent.settings.services.runtime_config_env import (
        apply_runtime_config_environment,
    )
    from agent_py_agent.agent.settings.services.runtime_config_task import (
        project_task_runtime_config_overlay,
    )

    base_path = tmp_path / "base.yaml"
    base_path.write_text(
        "memory_compact_trigger_percent: 80\nmemory_compact_auto_trigger_percent: 70\n",
        encoding="utf-8",
    )
    base = load_config(base_path)
    with pytest.raises(TypeError):
        replace(base, memory_compact_trigger_percent=canonical_target, memory_compact_auto_trigger_percent=85)
    direct = replace(base, memory_compact_trigger_percent=canonical_target)
    assert direct.memory_compact_trigger_percent == canonical_target

    overlay_path = tmp_path / f"overlay_{canonical_target}.yaml"
    overlay_path.write_text(
        f"memory_compact_trigger_percent: {canonical_target}\nmemory_compact_auto_trigger_percent: 85\n",
        encoding="utf-8",
    )
    task = SimpleNamespace(
        runtime_identity=SimpleNamespace(config_overlay_ref=str(overlay_path), config_scope="run")
    )
    projected = project_task_runtime_config_overlay(base, task, workspace_root=tmp_path)
    assert projected.memory_compact_trigger_percent == canonical_target
    assert not hasattr(projected, "memory_compact_auto_trigger_percent")

    env_applied = apply_runtime_config_environment(
        base,
        env={"MY_AGENT_RUNTIME_CONFIG": str(overlay_path)},
    )
    assert env_applied.memory_compact_trigger_percent == canonical_target
    assert not hasattr(env_applied, "memory_compact_auto_trigger_percent")


# LLM: replace或copy.replace同时显式传入host_home_snapshot与正式比例时，不能丢失显式新键，且实例__dict__不残留私有临时键。
# 函数用途: 验证无论是否并列覆盖host_home_snapshot，显式memory_compact_trigger_percent在70/80下均保持权威且源对象无状态污染。
@pytest.mark.parametrize("canonical_target", [70, 80])
@pytest.mark.parametrize("include_snapshot", [False, True])
def test_r9_explicit_canonical_independent_of_host_snapshot_and_clean_dict(
    canonical_target,
    include_snapshot,
):
    import copy
    from dataclasses import asdict

    cfg = AgentConfig(memory_compact_trigger_percent=70)
    base_keys = set(vars(cfg))
    assert "_compact_replace_trigger_copied" not in base_keys
    assert "_compact_trigger_previous_auto" not in base_keys
    overrides = {"memory_compact_trigger_percent": canonical_target}
    if include_snapshot:
        overrides["host_home_snapshot"] = None
    result = replace(cfg, **overrides)
    assert result.memory_compact_trigger_percent == canonical_target
    if hasattr(copy, "replace"):
        copy_result = copy.replace(cfg, **overrides)
        assert copy_result.memory_compact_trigger_percent == canonical_target
    assert cfg.memory_compact_trigger_percent == 70
    _ = asdict(cfg)
    _ = cfg.host_home_snapshot
    assert set(vars(cfg)) == base_keys


# LLM: replace遍历字段期间若有其他线程并发调用configured_home_root或读取源配置属性，不能干扰当前线程replace对正式比例的复制。
# 函数用途: 使用确定性线程交错验证只读并发访问源AgentConfig不会破坏正式比例的replace结果。
@pytest.mark.parametrize("concurrent_home_read", [False, True])
def test_r9_concurrent_readonly_home_resolution_does_not_affect_replace(
    concurrent_home_read,
):
    import dataclasses
    import sys
    import threading

    from agent_py_agent.agent.user_space.home_root import configured_home_root

    cfg = AgentConfig(memory_compact_trigger_percent=70)
    reached, resume = threading.Event(), threading.Event()
    results, errors = [], []
    paused = False
    replace_loop_code = getattr(dataclasses, "_replace", dataclasses.replace).__code__

    # LLM: 仅在测试线程执行到dataclasses.replace/_replace的正式比例字段时暂停一次，放行主线程执行并发只读访问。
    # 函数用途: 为并发replace测试提供确定性的线程交错点。
    def trace(frame, event, arg):
        nonlocal paused
        if (
            not paused
            and event == "line"
            and frame.f_code is replace_loop_code
            and getattr(frame.f_locals.get("f"), "name", None)
            == "memory_compact_trigger_percent"
        ):
            paused = True
            reached.set()
            assert resume.wait(5), "test schedule did not resume"
        return trace

    # LLM: 在受控trace下调用dataclasses.replace仅更新正式比例字段，收集克隆结果或异常。
    # 函数用途: 模拟工作线程对共享基础配置执行replace克隆。
    def clone():
        sys.settrace(trace)
        try:
            results.append(replace(cfg, memory_compact_trigger_percent=85))
        except BaseException as exc:
            errors.append(exc)
        finally:
            sys.settrace(None)

    worker = threading.Thread(target=clone)
    worker.start()
    try:
        assert reached.wait(5), "real dataclasses.replace did not reach the canonical trigger field"
        if concurrent_home_read:
            assert configured_home_root(cfg, env={}) is None
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert len(results) == 1
    assert results[0].memory_compact_trigger_percent == 85
    assert cfg.memory_compact_trigger_percent == 70


# 函数用途: 造一个只带窗口与三项压缩配置的轻量 agent，给 runtime_compact_policy 用。
def _agent(cap, *, percent=90, recovery=60):
    return SimpleNamespace(
        config=SimpleNamespace(
            memory_compact_trigger_percent=percent,
            memory_compact_recovery_target_percent=recovery,
            memory_compact_auto_trigger_max_tokens=cap,
        ),
        backend=SimpleNamespace(context_window_tokens=WINDOW),
    )


# 函数用途: 取一次策略里与触发线相关的全部数值，便于整体比较。
def _limits(policy):
    return (policy.context_window_tokens, policy.trigger_percent, policy.trigger_tokens,
            policy.recovery_target_tokens, policy.recent_tail_tokens)


def test_explicit_zero_keeps_the_policy_uncapped_and_invalid_falls_back_to_default_cap():
    uncapped = runtime_compact_policy(_agent(0))
    assert _limits(uncapped) == (WINDOW, 90, 900_000, 600_000, 20_000) and uncapped.trigger_capped is False
    default_capped = runtime_compact_policy(_agent(DEFAULT_COMPACT_TRIGGER_MAX_TOKENS))
    missing = runtime_compact_policy(SimpleNamespace(
        config=SimpleNamespace(memory_compact_trigger_percent=90, memory_compact_recovery_target_percent=60),
        backend=SimpleNamespace(context_window_tokens=WINDOW),
    ))
    assert _limits(missing) == _limits(default_capped) and missing.trigger_capped is True
    for cap in (None, "abc", -5, True, ""):
        policy = runtime_compact_policy(_agent(cap))
        assert _limits(policy) == _limits(default_capped), cap
        assert policy.trigger_capped is True
    assert [compact_trigger_max_tokens(value) for value in (0, None, "abc", -5, True, "300000", 250_000)] == [
        0, 300_000, 300_000, 300_000, 300_000, 300_000, 250_000]


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


def test_config_parsing_accepts_explicit_values_and_falls_back_to_safe_default():
    # 默认 300000（10-01 起）：生产曾被模型 reset 回旧默认 0，大窗口模型拖到约 90% 才压缩；显式 0 仍表示不封顶。
    assert AgentConfig().memory_compact_auto_trigger_max_tokens == DEFAULT_COMPACT_TRIGGER_MAX_TOKENS == 300_000
    settings, warnings = normalize_memory_settings({})
    assert settings.memory_compact_auto_trigger_max_tokens == 300_000 and warnings == []
    settings, warnings = normalize_memory_settings({"memory_compact_auto_trigger_max_tokens": "250000"})
    assert settings.memory_compact_auto_trigger_max_tokens == 250_000 and warnings == []
    settings, warnings = normalize_memory_settings({"memory_compact_auto_trigger_max_tokens": 0})
    assert settings.memory_compact_auto_trigger_max_tokens == 0 and warnings == []
    for raw in ("abc", -1, True):
        settings, warnings = normalize_memory_settings({"memory_compact_auto_trigger_max_tokens": raw})
        assert settings.memory_compact_auto_trigger_max_tokens == 300_000, raw
        assert [item.field_name for item in warnings] == ["memory_compact_auto_trigger_max_tokens"], raw
        assert compact_trigger_max_tokens(raw) == 300_000, raw
    assert compact_trigger_max_tokens(None) == 300_000 and compact_trigger_max_tokens(0) == 0


@pytest.mark.parametrize("cap", [0, 300_000])
def test_request_preflight_triggers_at_the_cap(monkeypatch, cap):
    agent = SimpleNamespace(
        config=AgentConfig(
            auto_save_memory=True, model_context_window_tokens=WINDOW,
            memory_compact_trigger_percent=90, memory_compact_auto_trigger_max_tokens=cap,
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


# 后台定时回合：所选档案窗口 1M、默认 80%，只有上限把触发线压到 17,550 时，
# 首个后台请求前先压缩；不封顶的对照组直接发业务请求、不压缩。
@pytest.mark.parametrize("cap", [0, 17_550])
def test_background_scheduled_turn_precheck_compacts_at_the_cap(tmp_path, monkeypatch, cap):
    agent, store, thread, request, execution, sink = _background(
        tmp_path, backend="openai_compatible", detached=False, window=WINDOW,
    )
    assert request.reason == "scheduled_progress_report"
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


@pytest.mark.parametrize(("cap", "expected"), [(0, 0), (950_000, 0), (800_000, 0), (300_000, 300_000)])
def test_finalization_cycle_receives_the_token_trigger_only_when_capped(tmp_path, monkeypatch, cap, expected):
    options = _cycle_options(tmp_path, monkeypatch, cap)

    assert options.trigger_percent == 80 and options.max_context_tokens == WINDOW
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
    (0, 800_000, False), (800_000, 800_000, False), (300_000, 300_000, True),
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
    percent = set_tunable_value("memory_compact_trigger_percent", "80", user_path=user)

    assert cap["ok"] is True and cap["saved"] == "300000" and cap["written_value_matches"] is True
    assert cap["effect_text"] == percent["effect_text"] and "重启 Gateway 才生效" in cap["effect_text"]
    assert set_tunable_value("memory_compact_auto_trigger_max_tokens", "-1", user_path=user)["ok"] is False
