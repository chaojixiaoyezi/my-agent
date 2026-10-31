# LLM: 显式90%容量夹具隔离生产默认80%；保留原校准、完整请求、输出预留及发送断言，只有模型/HTTP末端为替身。
# 模块用途: 验证候选接受门与Gateway预检共用冻结供应商观测，不误拒有效候选或丢失失败计量。
"""压缩候选接受门与预检共用同一校准口径（2026-09-28，压缩异常②回归）。

背景：真机 2026-09-28 单回合多次 read_file 之后，预检按供应商观测校准过的可见上下文越过触发线，
恢复压缩却按未校准的本地估算量候选（估算比供应商实际高约 43%），候选被 COMPACT_CANDIDATE_TOO_LARGE 拒掉；
压缩开始时线程 model_context_usage 快照又被压缩前的原始估算覆盖，事后无法判断真实占用。
锁定：
- 纯校准函数：候选比观测请求小时按同一观测比例折算（下限 50%），候选更大时沿预检的追加口径；无观测原样返回。
- 宿主边界冻结适用观测（同 fingerprint、同代次），投影仍是纯函数；表面或代次不符、缺观测都回到原始口径。
- transcript 与活动回合两条接受门都用校准值与输入上限比较：估算偏高 40% 时候选被接受；恰好等于上限仍拒绝；
  输出预留照旧生效；未校准时行为不变。
- 提交后下一次真实预检与接受基准一致：已接受的候选不会立刻按另一口径再压一次；失败路径的线程快照不被原始值误导。
- conversation_compaction_progress 的 started 事件带 trigger_source（preflight / provider_error /
  tool_context_overflow），compact_progress 白名单同步；工具窗口溢出是结构化来源，不靠 detail 文本区分。
只替身摘要模型与 HTTP 末端；计量、候选、checkpoint、CAS、进度和快照都走生产链。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import compact_request_recovery as recovery
from agent_py_agent.agent.agent_core import tool_model_generation
from agent_py_agent.agent.agent_core.model.context_pressure import (
    _provider_calibrated_context_tokens,
    preflight_context_pressure_response,
    projected_model_context_components,
)
from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.http import HttpBackend
from agent_py_agent.agent.conversation import compact as compact_module
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_progress import (
    CONVERSATION_COMPACT_PROGRESS_SCHEMA,
    normalize_conversation_compact_progress,
)
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import request_context, request_execution
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests import test_active_turn_compact_projection as active_turn_tests
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot
from agent_py_agent.tests.test_compact_capacity_facts import _transcript_case
from agent_py_agent.tests.test_gateway_compact_recovery import _history_request

# 复用活动回合投影用例的隔离环境：真实 Store/checkpoint/CAS，只替身摘要模型。
carried_case = active_turn_tests.carried_case

_SURFACE = "a" * 64
# 观测：本地估算 10_000，供应商实际 7_000，即估算偏高约 43%（真机 242,207 对 169,217 的同一形状）。
_OBSERVED_RAW = 10_000
_OBSERVED_PROVIDER = 7_000


# LLM: 只构造合成且类型化的冻结校准事实，作用域由参数显式提供，不写线程或伪装真实供应商观测。
# 函数用途: 提供本地10000与供应商7000口径的测试校准快照。
def _calibration(scope: str = "current_run"):
    from agent_py_agent.agent.conversation.compact_calibration import CompactRequestCalibration

    return CompactRequestCalibration(
        raw_estimated_tokens=_OBSERVED_RAW, provider_input_tokens=_OBSERVED_PROVIDER,
        context_surface_fingerprint=_SURFACE, compact_generation=0, calibration_scope=scope,
    )


# LLM: 生成结构化供应商观测载荷供真实冻结函数校验，覆盖项只属于测试输入，不绕过身份或代次检查。
# 函数用途: 构造可控制表面、代次及作用域的合成观测。
def _observation(**overrides):
    return {
        "schema": "provider_context_observation.v3", "raw_estimated_tokens": _OBSERVED_RAW,
        "provider_input_tokens": _OBSERVED_PROVIDER, "context_surface_fingerprint": _SURFACE,
        "observed_at": 1.0, **overrides,
    }


# ---------------------------------------------------------------------------
# 纯校准函数：候选口径与预检口径的关系
# ---------------------------------------------------------------------------


# LLM: 候选缩小时按冻结比例，大于观测请求时按追加口径；两种作用域都运行真实校准函数。
# 函数用途: 验证候选校准的比例、增长及相等边界。
@pytest.mark.parametrize("raw,scope,expected", [
    # 候选比观测请求小：按观测比例折算（8_000 × 0.7 = 5_600），两种作用域一致。
    (8_000, "current_run", 5_600),
    (8_000, "durable_thread", 5_600),
    # 候选比观测请求大：沿预检的追加口径，超出部分按全价计（7_000 + 2_000）。
    (12_000, "current_run", 9_000),
    (12_000, "durable_thread", 9_000),
    # 恰好等于观测请求：就是供应商实际值。
    (10_000, "current_run", 7_000),
])
def test_calibrated_candidate_tokens_follow_the_same_observation(raw, scope, expected) -> None:
    from agent_py_agent.agent.conversation.compact_calibration import (
        calibrated_compact_request_tokens,
    )

    assert calibrated_compact_request_tokens(raw, _calibration(scope)) == expected


# LLM: 缺少或非法供应商观测时只能保留原始估算，不能凭缺失事实推导优惠比例。
# 函数用途: 验证无可用校准时不改变候选计量。
def test_calibrated_candidate_tokens_keep_raw_without_usable_observation() -> None:
    from agent_py_agent.agent.conversation.compact_calibration import (
        CompactRequestCalibration,
        calibrated_compact_request_tokens,
    )

    assert calibrated_compact_request_tokens(8_000, None) == 8_000
    for raw_observed, provider in ((0, 7_000), (10_000, 0), (-1, 7_000)):
        calibration = CompactRequestCalibration(
            raw_estimated_tokens=raw_observed, provider_input_tokens=provider,
            context_surface_fingerprint=_SURFACE, compact_generation=0, calibration_scope="current_run",
        )
        assert calibrated_compact_request_tokens(8_000, calibration) == 8_000


# LLM: 校准折算保守下限保持50%，耐久作用域增长仍取规定口径的较大值，不修改生产公式。
# 函数用途: 验证极小供应商比例不会把候选不合理压低。
def test_calibrated_candidate_tokens_never_scale_below_half() -> None:
    from agent_py_agent.agent.conversation.compact_calibration import (
        CompactRequestCalibration,
        calibrated_compact_request_tokens,
    )

    # 观测比例 0.2 时保守下限仍是 50%：8_000 → 4_000，不按 0.2 折成 1_600。
    calibration = CompactRequestCalibration(
        raw_estimated_tokens=10_000, provider_input_tokens=2_000,
        context_surface_fingerprint=_SURFACE, compact_generation=0, calibration_scope="durable_thread",
    )
    assert calibrated_compact_request_tokens(8_000, calibration) == 4_000
    # 更大时耐久作用域取“比例折算”与“追加口径”的较大者：max(12_000 × 0.5, 2_000 + 2_000) = 6_000。
    assert calibrated_compact_request_tokens(12_000, calibration) == 6_000


# LLM: 真实预检和候选函数对增长请求共用口径；缩小请求的刻意差异单独锁定，不能直接复用增长函数。
# 函数用途: 验证增长校准一致并保留缩小候选的比例折算。
def test_candidate_scale_matches_preflight_for_growth_and_documents_shrink_divergence() -> None:
    from agent_py_agent.agent.conversation.compact_calibration import (
        calibrated_compact_request_tokens,
    )

    agent = SimpleNamespace()
    for scope in ("current_run", "durable_thread"):
        params = SimpleNamespace(
            live_archive_state={"_provider_context_observation": _observation(_calibration_scope=scope)},
            task_attributes={},
        )
        for raw in (10_000, 12_000, 25_000):
            assert _provider_calibrated_context_tokens(
                agent, params, raw, context_surface_fingerprint=_SURFACE,
            ) == calibrated_compact_request_tokens(raw, _calibration(scope))
    # 本轮追加口径对压小后的请求原样返回（Codex 审阅第 1 点）：候选门不能直接复用它，必须按比例折算。
    params = SimpleNamespace(
        live_archive_state={"_provider_context_observation": _observation(_calibration_scope="current_run")},
        task_attributes={},
    )
    assert _provider_calibrated_context_tokens(agent, params, 8_000, context_surface_fingerprint=_SURFACE) == 8_000
    assert calibrated_compact_request_tokens(8_000, _calibration("current_run")) == 5_600


# ---------------------------------------------------------------------------
# 宿主边界冻结适用观测：同 fingerprint、同代次；不符就没有校准
# ---------------------------------------------------------------------------


# LLM: 使用临时真实ConversationStore和最小代理配置，只隔离模型后端，不替换观测存储及冻结逻辑。
# 函数用途: 准备验证冻结校准身份和代次的本地会话。
def _freeze_agent(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "calibration-freeze"})
    agent = SimpleNamespace(
        config=AgentConfig(auto_save_memory=True, model_context_window_tokens=100_000),
        backend=SimpleNamespace(context_window_tokens=100_000, name="fake", model_name="fake-model"),
        conversation_store=store,
    )
    return agent, store, thread


# LLM: 参数显式绑定准确线程和真实测试协议快照，本轮观测载荷不被复制到另一权威来源。
# 函数用途: 为校准冻结入口准备会话作用域及可选运行态观测。
def _freeze_params(thread, state=None):
    return SimpleNamespace(
        context_scope="conversation", save=True,
        task_attributes={"conversation_thread_id": thread.thread_id},
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
        live_archive_state={} if state is None else state,
    )


# LLM: 本轮观测只在准确请求表面匹配时冻结，冻结必须纯读取，不得反写观测或跨表面复用。
# 函数用途: 验证本轮校准的同表面准入和只读性。
def test_frozen_calibration_uses_current_run_observation_with_same_surface(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.model.context_pressure import (
        frozen_compact_request_calibration,
    )

    agent, _store, thread = _freeze_agent(tmp_path)
    params = _freeze_params(thread, {
        "_provider_context_observation": _observation(_calibration_scope="current_run"),
    })
    frozen = frozen_compact_request_calibration(agent, params, context_surface_fingerprint=_SURFACE)
    assert frozen == _calibration("current_run")
    assert frozen_compact_request_calibration(agent, params, context_surface_fingerprint="b" * 64) is None
    # 冻结是纯读取：不改写本轮观测，也不把别的表面写进状态。
    assert params.live_archive_state["_provider_context_observation"] == _observation(_calibration_scope="current_run")


# LLM: 耐久观测通过真实存储写入，只有当前压缩代可复用；缺失或其它代不得补造校准。
# 函数用途: 验证持久供应商观测的同代次冻结边界。
def test_frozen_calibration_hydrates_durable_observation_only_for_current_generation(tmp_path) -> None:
    from agent_py_agent.agent.agent_core.model.context_pressure import (
        frozen_compact_request_calibration,
    )

    agent, store, thread = _freeze_agent(tmp_path)
    assert frozen_compact_request_calibration(agent, _freeze_params(thread), context_surface_fingerprint=_SURFACE) is None
    store.threads.update_provider_context_observation(
        thread.thread_id, _observation(compact_generation=0), expected_compact_generation=0,
    )
    frozen = frozen_compact_request_calibration(agent, _freeze_params(thread), context_surface_fingerprint=_SURFACE)
    assert frozen == _calibration("durable_thread")
    # 代次不符（记录的是别的压缩代）：没有校准，不能静默复用旧代次观测。
    store.threads.update_provider_context_observation(
        thread.thread_id, _observation(compact_generation=1), expected_compact_generation=0,
    )
    assert frozen_compact_request_calibration(agent, _freeze_params(thread), context_surface_fingerprint=_SURFACE) is None


# ---------------------------------------------------------------------------
# transcript 接受门（会话历史压缩）
# ---------------------------------------------------------------------------

# _transcript_case：窗口 10_000、输出上限 4_000、触发 90%，输入上限 = min(9_000, 6_000) = 6_000，恢复目标 6_000。
_TRANSCRIPT_CEILING = 6_000


# LLM: 只提供受控完整请求计量，不替换校准或接受门；候选与压缩前由结构化is_candidate区分。
# 函数用途: 构造可独立设置压缩前与候选大小的测试投影器。
def _transcript_projector(candidate_raw: int, before_raw: int = 12_000):
    # LLM: 根据真实候选视图返回完整材料占位和显式计量，不提交状态或解析摘要文案。
    # 函数用途: 为会话容量门提供确定的两阶段请求大小。
    def project(view):
        return ConversationCompactProjection(candidate_raw if view.is_candidate else before_raw, object())

    return project


# LLM: 只按结构化phase匹配进度，要求恰好一个事件，缺失或重复都失败而非选最新掩盖问题。
# 函数用途: 取出待核对的唯一压缩阶段事件。
def _phase(events, phase):
    matched = [event for event in events if event.get("phase") == phase]
    assert len(matched) == 1, (phase, events)
    return matched[0]


# LLM: 摘要文本替身不改变原容量门；真实提交、检查点及进度必须共用校准后的数值而保留纯投影原值。
# 函数用途: 验证原估算过大但校准后可容纳的会话候选被接受。
def test_transcript_gate_accepts_candidate_under_calibrated_ceiling(tmp_path, monkeypatch) -> None:
    agent, store, thread = _transcript_case(tmp_path, monkeypatch, turns=3)
    monkeypatch.setattr(compact_module, "_summarize", lambda *args, **kwargs: "新摘要")
    events = []
    result = prepare_conversation_context(agent, store, thread, options=ConversationCompactOptions(
        current_prompt="继续完整任务", progress_callback=events.append,
        request_projector=_transcript_projector(8_000), calibration=_calibration(), trigger_source="preflight",
    ))
    # 原始候选 8_000 ≥ 上限 6_000，但按观测折算为 5_600 < 6_000：接受，并按校准值记账。
    assert result.compacted and result.projected_tokens == 5_600
    assert result.request_projection.projected_tokens == 8_000  # 纯投影保留原始计量，不被校准改写。
    checkpoint, = committed_compact_checkpoint_chain(agent, result.thread)
    assert checkpoint["projected_tokens_before"] == 9_000  # 12_000 沿预检追加口径：7_000 + 2_000。
    assert checkpoint["projected_tokens_after"] == 5_600
    started = _phase(events, "started")
    assert started["before_tokens"] == 9_000 and started["trigger_source"] == "preflight"
    assert normalize_conversation_compact_progress(started)["trigger_source"] == "preflight"
    assert _phase(events, "completed")["after_tokens"] == 5_600
    assert store.threads.load(thread.thread_id).compact_generation == 1


# LLM: 未传冻结校准时真实接受门仍以原始值拒绝过大候选，失败记录及进度不可伪造折算。
# 函数用途: 验证没有观测的会话压缩保留原始计量和类型化失败。
def test_transcript_gate_keeps_raw_measurement_without_calibration(tmp_path, monkeypatch) -> None:
    agent, store, thread = _transcript_case(tmp_path, monkeypatch, turns=3)
    monkeypatch.setattr(compact_module, "_summarize", lambda *args, **kwargs: "新摘要")
    events = []
    with pytest.raises(ConversationCompactError) as error:
        prepare_conversation_context(agent, store, thread, options=ConversationCompactOptions(
            current_prompt="继续完整任务", progress_callback=events.append,
            request_projector=_transcript_projector(8_000),
        ))
    assert error.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    assert error.value.capacity.candidate_tokens == 8_000
    assert error.value.capacity.input_ceiling_tokens == _TRANSCRIPT_CEILING
    assert _phase(events, "started")["before_tokens"] == 12_000
    assert store.threads.load(thread.thread_id).compact_failure_code == "COMPACT_CANDIDATE_TOO_LARGE"


# LLM: 候选必须严格小于真实输入上限，输出预留仍生效，拒绝后代数不前进。
# 函数用途: 验证会话候选校准后到线或越线仍不能提交。
@pytest.mark.parametrize("candidate_raw,calibrated", [
    (8_570, 5_999),
    (8_571, 6_000),  # 折算后恰好等于输入上限：仍拒绝。
    (10_000, 7_000),  # 折算后低于触发线 9_000 但超过“窗口 − 输出预留”：输出预留照旧生效。
])
def test_transcript_gate_rejects_calibrated_candidate_at_or_over_ceiling(
    tmp_path, monkeypatch, candidate_raw, calibrated,
) -> None:
    agent, store, thread = _transcript_case(tmp_path, monkeypatch, turns=3)
    monkeypatch.setattr(compact_module, "_summarize", lambda *args, **kwargs: "新摘要")
    options = ConversationCompactOptions(
        current_prompt="继续完整任务", request_projector=_transcript_projector(candidate_raw), calibration=_calibration(),
    )
    if calibrated < _TRANSCRIPT_CEILING:
        result = prepare_conversation_context(agent, store, thread, options=options)
        assert result.compacted and result.projected_tokens == calibrated
        return
    with pytest.raises(ConversationCompactError) as error:
        prepare_conversation_context(agent, store, thread, options=options)
    assert error.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    assert error.value.capacity.candidate_tokens == calibrated
    assert error.value.capacity.input_ceiling_tokens == _TRANSCRIPT_CEILING
    assert store.threads.load(thread.thread_id).compact_generation == 0


# ---------------------------------------------------------------------------
# 活动回合接受门（carried tool archive）
# ---------------------------------------------------------------------------


# LLM: 复用显式90%的真实活动归档夹具，完整投影原值、冻结校准及检查点必须各保口径不混写。
# 函数用途: 验证活动回合候选经校准后可在原输入上限内提交。
def test_active_turn_gate_accepts_candidate_under_calibrated_ceiling(carried_case) -> None:
    case = carried_case
    projections = []

    # LLM: 只记录带同一摘要的完整候选投影，原始9500计量不能被后续校准就地覆盖。
    # 函数用途: 提供估算偏高但校准后可接受的活动候选。
    def project(summary, _retained, _generation):
        projection = ConversationCompactProjection(9_500, {"summary": summary})
        projections.append(projection)
        return projection

    result = active_turn_tests._compact(
        case, request_projector=project, projected_tokens_before=20_000, calibration=_calibration(),
        trigger_source="tool_context_overflow", progress_callback=case.progress.append,
    )
    # 上限 = 触发线 9_000；原始候选 9_500 ≥ 9_000，比观测请求小、按比例折算 6_650 < 9_000：接受。
    # 压缩前 20_000 比观测请求大，沿预检追加口径 → 7_000 + 10_000 = 17_000。
    assert result.compacted and result.projected_tokens_after == 6_650
    assert result.projected_tokens_before == 17_000
    assert result.request_projection is projections[-1] and projections[-1].projected_tokens == 9_500
    checkpoint, = committed_compact_checkpoint_chain(case.agent, result.thread)
    assert checkpoint["projected_tokens_before"] == 17_000
    assert checkpoint["projected_tokens_after"] == 6_650
    started = _phase(case.progress, "started")
    assert started["trigger_source"] == "tool_context_overflow"
    assert {event["before_tokens"] for event in case.progress} == {17_000}
    assert _phase(case.progress, "completed")["after_tokens"] == 6_650


# LLM: 显式90%输入上限与真实输出预留联合约束候选，严格小于才接受；不得为默认变化改弱断言。
# 函数用途: 验证活动回合候选校准后的输入与输出预留边界。
@pytest.mark.parametrize("output_cap,candidate_raw,calibrated", [
    # 上限 9_000（触发线）：候选比观测请求大，沿追加口径 7_000 + (raw − 10_000)。
    (0, 11_999, 8_999),
    (0, 12_000, 9_000),  # 折算后恰好等于触发线上限：拒绝。
    # 输出预留把上限压到 3_000：候选比观测请求小，按比例折算 ceil(raw × 0.7)。
    (7_000, 4_284, 2_999),
    (7_000, 4_285, 3_000),  # 折算后恰好等于上限仍拒绝。
    (7_000, 4_286, 3_001),
])
def test_active_turn_gate_rejects_at_ceiling_and_keeps_output_reserve(
    carried_case, output_cap, candidate_raw, calibrated,
) -> None:
    case = carried_case
    if output_cap:
        case.agent.backend = HttpBackend(BackendOptions(
            api_base="https://example.invalid", api_key="", model_name="offline-capacity",
            max_tokens=output_cap, context_window_tokens=10_000,
        ))
    project = lambda *_: ConversationCompactProjection(candidate_raw, object())  # noqa: E731
    if calibrated < (3_000 if output_cap else 9_000):
        result = active_turn_tests._compact(case, request_projector=project, calibration=_calibration())
        assert result.compacted and result.projected_tokens_after == calibrated
        return
    with pytest.raises(ConversationCompactError) as error:
        active_turn_tests._compact(case, request_projector=project, calibration=_calibration())
    assert error.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    assert error.value.capacity.candidate_tokens == calibrated
    assert error.value.capacity.input_ceiling_tokens == (3_000 if output_cap else 9_000)
    active_turn_tests._assert_uncommitted(case)


# ---------------------------------------------------------------------------
# 触发来源：started 事件、白名单、工具窗口溢出的结构化来源
# ---------------------------------------------------------------------------


# LLM: 只构造结构化进度测试载荷，覆盖项用于白名单反例，不把文字当触发来源。
# 函数用途: 生成可控制触发来源的压缩开始事件。
def _progress(**overrides):
    return {
        "schema": CONVERSATION_COMPACT_PROGRESS_SCHEMA, "phase": "started", "stage": "preparing", "percent": 5,
        "generation": 2, "operation_id": "compact:test", "before_tokens": 120_000, "after_tokens": 0,
        "trigger_tokens": 115_200, "source_messages": 80, "source_kind": "conversation_transcript",
        "commit_authority": "conversation_thread", **overrides,
    }


# LLM: 触发来源只接受公开结构化枚举，缺失或自然语言不得由进度规范化器补成机器事实。
# 函数用途: 验证开始进度只保留已知压缩触发来源。
def test_compact_progress_whitelist_keeps_only_known_trigger_sources() -> None:
    from agent_py_agent.agent.conversation.compact_progress import COMPACT_TRIGGER_SOURCES

    assert frozenset({"preflight", "provider_error", "tool_context_overflow"}) == COMPACT_TRIGGER_SOURCES
    for source in sorted(COMPACT_TRIGGER_SOURCES):
        assert normalize_conversation_compact_progress(_progress(trigger_source=source))["trigger_source"] == source
    assert "trigger_source" not in normalize_conversation_compact_progress(_progress())
    assert "trigger_source" not in normalize_conversation_compact_progress(_progress(trigger_source="用户说要压缩"))


# LLM: 工具窗口超限由运行态结构化事实触发真实预检，不能从展示detail文本猜测来源。
# 函数用途: 验证工具上下文溢出的状态及触发来源准确透传。
def test_tool_context_window_overflow_is_a_structured_trigger_source() -> None:
    params = SimpleNamespace(
        context_scope="default",
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
        live_archive_state={"tool_context_window_overflow": {"omitted_count": 12, "original_chars": 80_000}},
    )
    agent = SimpleNamespace(
        config=AgentConfig(auto_save_memory=True), backend=SimpleNamespace(context_window_tokens=128_000, name="fake"),
    )
    response = preflight_context_pressure_response(SimpleNamespace(agent=agent, params=params, prompt="短 prompt"))
    assert response is not None and response.runtime_status == "context_overflow"
    assert response.runtime_source == "tool_context_overflow"
    assert "tool_context_window_overflow=true" in response.text


# ---------------------------------------------------------------------------
# Gateway 全链：假 LLM 复现异常②，两回合，隔离 home
# ---------------------------------------------------------------------------

_WINDOW = 90_000
_OUTPUT = 10_000
# 输入上限 = min(90% × 90_000 = 81_000, 90_000 − 10_000 = 80_000)。
_GATEWAY_CEILING = 80_000
# 供应商实际 = 本地估算 × 0.7，即估算偏高约 43%。
_RATIO = 0.7
# 系统提示固定填充（约 15K tokens），让固定开销够大：候选原始计量越过上限，折算后才能放下。
_REQUIREMENT = "CURRENT_REQUIREMENT_BEGIN " + "a" * 45_000 + " CURRENT_REQUIREMENT_END"
_SUMMARY_TEXT = "旧资料已完成核对，后续遵守当前完整要求。"


# LLM: 只收集公开结构化进度及用量投影，不渲染正文、不修改原线程或进度规范化合同。
# 类用途: 为本地Gateway回归保存可核对的压缩事件和上下文数字。
class _ProgressSink:
    """只记录结构化进度与上下文数字的 on_chunk 替身；不渲染正文。"""

    # LLM: 每例独立持有空事件列表，不复用其它回合或其它测试的观测。
    # 函数用途: 初始化进度和用量收集容器。
    def __init__(self) -> None:
        self.progress: list[dict[str, object]] = []
        self.usage: list[dict[str, object]] = []

    # LLM: 保存原进度字段的浅拷贝，返回写入成功只属于测试sink，不代表业务压缩成功。
    # 函数用途: 收集一次公开压缩进度事件。
    def write_conversation_compact_progress(self, value):
        self.progress.append(dict(value))
        return True

    # LLM: 只收集实际用量投影，不替换原观测、校准或线程快照。
    # 函数用途: 收集一次公开上下文用量事件。
    def write_context_usage(self, usage):
        self.usage.append(dict(usage))
        return True


# LLM: 仅替换供应商观测输入量后调用原记录器，表面身份和校准落库继续真实执行；None代表无可用usage。
# 函数用途: 按显式比例构造可检查的本地供应商用量观测。
def _calibrated_provider(monkeypatch, ratio):
    """让每次真实模型响应的 usage 变成“本地估算 × ratio”；ratio 为 None 时不给可用 usage（无观测）。"""
    raws = []
    original = tool_model_generation.record_provider_context_observation

    # LLM: 保留原响应及表面参数，只按测试比例替换usage数值并沿原入口写观测，不伪造线程代数。
    # 函数用途: 捕获真实原始估算并模拟供应商实际输入量。
    def record(agent, params, *, raw_estimated_tokens, context_surface_fingerprint, response):
        raws.append(raw_estimated_tokens)
        usage = {
            key: value for key, value in dict(getattr(response, "usage", None) or {}).items()
            if key not in ("cache_read_input_tokens", "cache_creation_input_tokens")
        }
        usage["input_tokens"] = int(raw_estimated_tokens * ratio) if ratio is not None else 0
        response.usage = usage
        return original(
            agent, params, raw_estimated_tokens=raw_estimated_tokens,
            context_surface_fingerprint=context_surface_fingerprint, response=response,
        )

    monkeypatch.setattr(tool_model_generation, "record_provider_context_observation", record)
    return raws


# LLM: 只替换HTTP末端，真实请求投影、协议探针和Gateway恢复链仍运行，所有响应为合成本地数据。
# 函数用途: 收集业务请求并提供不访问外网的供应商响应。
def _fake_provider_http(monkeypatch):
    business = []

    # LLM: 按结构化工具名区分合成协议探针与业务请求，不改变真实序列化请求内容或访问真实供应商。
    # 函数用途: 返回探针确认或固定业务文本，并保存业务负载。
    def send(request):
        wire = json.loads(json.dumps(request.payload, ensure_ascii=False))
        names = [row.get("name") for row in wire.get("tools", [])]
        if names == ["my_agent_capability_probe"]:
            import re

            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(wire))[1]
            return {"content": [{"type": "tool_use", "id": "probe", "name": names[0], "input": {"nonce": nonce}}],
                    "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1}}
        business.append(wire)
        return {"content": [{"type": "text", "text": "资料整理完成。"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 5}}

    monkeypatch.setattr(http, "post_json", send)
    return business


# LLM: 正式触发键显式90%，输出预留仍10000；仅模型及HTTP末端替身，原接受门、观测和CAS不替换。
# 函数用途: 准备具有明确80000输入上限的两回合Gateway校准场景。
def _gateway_case(tmp_path, monkeypatch, *, ratio):
    fixture = _history_request(tmp_path, mode="disabled", tools=True, original_window=_WINDOW, history_repeat=500)
    agent = fixture.agent
    agent.config.max_tokens = _OUTPUT
    agent.backend.max_tokens = _OUTPUT
    agent.config.memory_compact_trigger_percent = 90
    agent.config.system_prompt += "\n" + _REQUIREMENT
    monkeypatch.setattr(compact_module, "_summarize", lambda *args, **kwargs: _SUMMARY_TEXT)
    befores, candidates = [], []
    original_before = recovery._full_request_tokens
    original_project = recovery._project_mixed_recovery_material

    # LLM: 捕获真实完整请求计量后原样返回，不调整数值来制造场景成立。
    # 函数用途: 记录恢复前完整请求的原始大小。
    def full_request_tokens(*args):
        value = original_before(*args)
        befores.append(value)
        return value

    # LLM: 调用原混合恢复投影，只记录结构化候选视图的真实完整请求大小，不替换投影材料。
    # 函数用途: 捕获被测Gateway恢复链构造的候选计量。
    def project(*args):
        value = original_project(*args)
        if args[1].is_candidate:
            candidates.append(projected_model_context_components(value.projection)[0])
        return value

    monkeypatch.setattr(recovery, "_full_request_tokens", full_request_tokens)
    monkeypatch.setattr(recovery, "_project_mixed_recovery_material", project)
    raws = _calibrated_provider(monkeypatch, ratio)
    business = _fake_provider_http(monkeypatch)
    return SimpleNamespace(
        fixture=fixture, agent=agent, thread_id=fixture.thread_id, raws=raws, business=business,
        befores=befores, candidates=candidates,
    )


# LLM: 测试直接调用业务执行入口，须按原准确任务身份结束其固定租约，不清理其它请求或绕过认领合同。
# 函数用途: 用真实claims终态入口释放本例请求的执行车道。
def _finish_turn(case, request_id: str) -> None:
    """按 Gateway 终态收尾的同一结构化调用释放该请求钉住的执行车道（测试直接调 _run_gateway_ask，不经请求终态）。"""
    case.agent.conversation_store.claims.finish({
        "thread_id": case.thread_id, "expected_task_id": f"gateway:{request_id}", "recover_same_task_only": True,
        "status": "finished", "runtime_facts": {"execution_source": "gateway", "request_id": request_id},
    })


# LLM: 只在隔离临时目录写第二回合处理中记录，沿原会话身份构造真实Gateway上下文，不复用旧attempt。
# 函数用途: 为恢复校准测试准备第二个已绑定业务请求。
def _second_turn(case, prompt: str, sink: _ProgressSink):
    fixture = case.fixture
    request = {
        "id": "calib-turn-2", "kind": "ask", "prompt": prompt, "status": "processing", "turn_phase": "open",
        "execution_attempt_id": "transport-two", "conversation": dict(fixture.context.request["conversation"]),
        "inject": list(fixture.context.request["inject"]),
    }
    path = fixture.paths.processing / "calib-turn-2.json"
    path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    return request_context.GatewayAskRunContext(
        case.agent, request, path, fixture.paths.responses / "calib-turn-2.json", request["id"], sink,
    )


# LLM: 校准只从真实线程持久观测读取，使用原表面及代次，不按期望结果反向构造大小。
# 函数用途: 读回第一回合落库的耐久校准事实。
def _durable_calibration(case):
    from agent_py_agent.agent.conversation.compact_calibration import CompactRequestCalibration

    observation = case.agent.conversation_store.threads.require(case.thread_id).provider_context_observation
    return CompactRequestCalibration(
        raw_estimated_tokens=observation["raw_estimated_tokens"],
        provider_input_tokens=observation["provider_input_tokens"],
        context_surface_fingerprint=observation["context_surface_fingerprint"],
        compact_generation=observation["compact_generation"], calibration_scope="durable_thread",
    )


# LLM: 第一回合真实执行后按原终态释放认领，再核持久观测；无观测场景必须明确保持空状态。
# 函数用途: 建立第二回合所需的真实历史和可选供应商校准。
def _first_turn(case, *, observed: bool):
    """第一回合：请求本身放得下，只留下供应商观测（有观测的场景），并把回合正文追加进会话；返回冻结口径的校准事实。"""
    assert request_execution._run_gateway_ask(case.fixture.context).response == "资料整理完成。"
    _finish_turn(case, case.fixture.context.request_id)
    assert len(case.business) == 1 and len(case.raws) == 1 and case.raws[0] < _GATEWAY_CEILING
    thread = case.agent.conversation_store.threads.require(case.thread_id)
    if not observed:
        assert thread.provider_context_observation == {}
        return None
    assert thread.provider_context_observation["compact_generation"] == 0
    assert thread.provider_context_observation["provider_input_tokens"] == int(case.raws[0] * _RATIO)
    return _durable_calibration(case)


# LLM: 预期值来自真实探针计量再调用原校准函数，必须同时验证原始越线及校准后是否装下的场景前提。
# 函数用途: 计算并核对压缩前及最小候选的真实口径大小。
def _expected_sizes(case, calibration, *, fits: bool):
    """按探针捕获的原始计量算出口径折算后的压缩前/候选大小，并核对场景前提（尺寸漂移在这里失败，而不是悄悄换了含义）。"""
    from agent_py_agent.agent.conversation.compact_calibration import (
        calibrated_compact_request_tokens,
    )

    before_raw, = case.befores
    candidate_raw = min(case.candidates)
    expected_before = calibrated_compact_request_tokens(before_raw, calibration)
    expected_candidate = calibrated_compact_request_tokens(candidate_raw, calibration)
    assert before_raw >= _GATEWAY_CEILING and candidate_raw >= _GATEWAY_CEILING, (before_raw, candidate_raw)
    assert expected_before >= _GATEWAY_CEILING, (before_raw, expected_before)
    assert (expected_candidate < _GATEWAY_CEILING) == fits, (candidate_raw, expected_candidate)
    return expected_before, expected_candidate


# LLM: 成功须同时核真实线程、公开进度、实际发送材料及下一次预检基线，不能只凭候选提交标志认定全链成功。
# 函数用途: 核对获选摘要按同一校准大小发送且不会立即再次触发压缩。
def _assert_recovery_sent_the_accepted_candidate(case, sink, expected_candidate):
    thread = case.agent.conversation_store.threads.require(case.thread_id)
    assert _phase(sink.progress, "completed")["after_tokens"] == expected_candidate
    assert thread.compact_generation == 1 and thread.summary == _SUMMARY_TEXT
    # 已提交候选按同一口径发送：只多一次业务发送，正文带摘要不带旧资料。
    assert len(case.business) == 2
    restored = json.dumps(case.business[-1], ensure_ascii=False)
    assert _SUMMARY_TEXT in restored and "之前核对过的原始资料" not in restored
    # 提交后的真实预检与接受基准一致：快照就是接受时的校准值，不会立刻再压一次。
    assert thread.model_context_usage["current_tokens"] == expected_candidate
    assert thread.model_context_usage["compact_generation"] == 1
    assert thread.provider_context_observation["compact_generation"] == 1


# LLM: 失败须保留原历史代次和业务发送数量，容量及用量快照仍用真实校准值，不得被原始估算覆盖。
# 函数用途: 核对拒绝候选后的失败计量、类型化错误及线程观测。
def _assert_failure_snapshot_is_calibrated(case, sink, expected_before, expected_candidate):
    thread = case.agent.conversation_store.threads.require(case.thread_id)
    failed = _phase(sink.progress, "failed")
    assert failed["error_code"] == "COMPACT_CANDIDATE_TOO_LARGE"
    assert failed["candidate_tokens"] == expected_candidate
    assert failed["input_ceiling_tokens"] == _GATEWAY_CEILING
    assert len(case.business) == 1 and thread.compact_generation == 0
    assert thread.compact_failure_code == "COMPACT_CANDIDATE_TOO_LARGE"
    # 失败路径的线程快照是压缩前的校准值（无观测时就是原始值），不被原始估算误导。
    assert thread.model_context_usage["current_tokens"] == expected_before
    assert [row["current_tokens"] for row in sink.usage] == [expected_before]


# LLM: 只调合成输入长度恢复阈值场景，不改生产预算或放宽校准/发送断言；继续走真实Gateway预检。
# 函数用途: 在默认提示缩短后仍验证候选使用与预检相同的校准口径。
@pytest.mark.parametrize("scenario", ["calibrated_fit", "calibrated_too_large", "no_observation"])
def test_gateway_recovery_measures_candidates_with_the_preflight_calibration(tmp_path, monkeypatch, scenario):
    case = _gateway_case(tmp_path, monkeypatch, ratio=None if scenario == "no_observation" else _RATIO)
    calibration = _first_turn(case, observed=scenario != "no_observation")
    # 第二回合：大段新需求让完整请求越过上限；候选原始计量也越过上限，只有按观测折算后才可能放下。
    # 默认工具延迟展开和提示精简后，增加合成新需求；具体压缩前/候选大小仍由_expected_sizes实测断言。
    # 保持80K输入上限、0.7校准率，以及「仅校准后放得下/校准后仍超限/无观测」三个场景。
    sink = _ProgressSink()
    context = _second_turn(case, "材料核对项。" * (11_500 if scenario == "calibrated_too_large" else 8_000), sink)
    fits = scenario == "calibrated_fit"
    if fits:
        assert request_execution._run_gateway_ask(context).response == "资料整理完成。"
    else:
        with pytest.raises((RuntimeError, ConversationCompactError)) as failure:
            request_execution._run_gateway_ask(context)
        assert getattr(failure.value, "error_code", None) == "COMPACT_CANDIDATE_TOO_LARGE"
    expected_before, expected_candidate = _expected_sizes(case, calibration, fits=fits)
    started = _phase(sink.progress, "started")
    assert started["before_tokens"] == expected_before and started["trigger_source"] == "preflight"
    assert started["source_kind"] == "conversation_transcript"
    if fits:
        _assert_recovery_sent_the_accepted_candidate(case, sink, expected_candidate)
    else:
        _assert_failure_snapshot_is_calibrated(case, sink, expected_before, expected_candidate)
