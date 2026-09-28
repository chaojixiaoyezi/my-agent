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


def _calibration(scope: str = "current_run"):
    from agent_py_agent.agent.conversation.compact_calibration import CompactRequestCalibration

    return CompactRequestCalibration(
        raw_estimated_tokens=_OBSERVED_RAW, provider_input_tokens=_OBSERVED_PROVIDER,
        context_surface_fingerprint=_SURFACE, compact_generation=0, calibration_scope=scope,
    )


def _observation(**overrides):
    return {
        "schema": "provider_context_observation.v3", "raw_estimated_tokens": _OBSERVED_RAW,
        "provider_input_tokens": _OBSERVED_PROVIDER, "context_surface_fingerprint": _SURFACE,
        "observed_at": 1.0, **overrides,
    }


# ---------------------------------------------------------------------------
# 纯校准函数：候选口径与预检口径的关系
# ---------------------------------------------------------------------------


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


def _freeze_agent(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "calibration-freeze"})
    agent = SimpleNamespace(
        config=AgentConfig(auto_save_memory=True, model_context_window_tokens=100_000),
        backend=SimpleNamespace(context_window_tokens=100_000, name="fake", model_name="fake-model"),
        conversation_store=store,
    )
    return agent, store, thread


def _freeze_params(thread, state=None):
    return SimpleNamespace(
        context_scope="conversation", save=True,
        task_attributes={"conversation_thread_id": thread.thread_id},
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="text"),
        live_archive_state={} if state is None else state,
    )


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


def _transcript_projector(candidate_raw: int, before_raw: int = 12_000):
    def project(view):
        return ConversationCompactProjection(candidate_raw if view.is_candidate else before_raw, object())

    return project


def _phase(events, phase):
    matched = [event for event in events if event.get("phase") == phase]
    assert len(matched) == 1, (phase, events)
    return matched[0]


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


def test_active_turn_gate_accepts_candidate_under_calibrated_ceiling(carried_case) -> None:
    case = carried_case
    projections = []

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


def _progress(**overrides):
    return {
        "schema": CONVERSATION_COMPACT_PROGRESS_SCHEMA, "phase": "started", "stage": "preparing", "percent": 5,
        "generation": 2, "operation_id": "compact:test", "before_tokens": 120_000, "after_tokens": 0,
        "trigger_tokens": 115_200, "source_messages": 80, "source_kind": "conversation_transcript",
        "commit_authority": "conversation_thread", **overrides,
    }


def test_compact_progress_whitelist_keeps_only_known_trigger_sources() -> None:
    from agent_py_agent.agent.conversation.compact_progress import COMPACT_TRIGGER_SOURCES

    assert frozenset({"preflight", "provider_error", "tool_context_overflow"}) == COMPACT_TRIGGER_SOURCES
    for source in sorted(COMPACT_TRIGGER_SOURCES):
        assert normalize_conversation_compact_progress(_progress(trigger_source=source))["trigger_source"] == source
    assert "trigger_source" not in normalize_conversation_compact_progress(_progress())
    assert "trigger_source" not in normalize_conversation_compact_progress(_progress(trigger_source="用户说要压缩"))


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


class _ProgressSink:
    """只记录结构化进度与上下文数字的 on_chunk 替身；不渲染正文。"""

    def __init__(self) -> None:
        self.progress: list[dict[str, object]] = []
        self.usage: list[dict[str, object]] = []

    def write_conversation_compact_progress(self, value):
        self.progress.append(dict(value))
        return True

    def write_context_usage(self, usage):
        self.usage.append(dict(usage))
        return True


def _calibrated_provider(monkeypatch, ratio):
    """让每次真实模型响应的 usage 变成“本地估算 × ratio”；ratio 为 None 时不给可用 usage（无观测）。"""
    raws = []
    original = tool_model_generation.record_provider_context_observation

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


def _fake_provider_http(monkeypatch):
    business = []

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


def _gateway_case(tmp_path, monkeypatch, *, ratio):
    fixture = _history_request(tmp_path, mode="disabled", tools=True, original_window=_WINDOW, history_repeat=500)
    agent = fixture.agent
    agent.config.max_tokens = _OUTPUT
    agent.backend.max_tokens = _OUTPUT
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.config.system_prompt += "\n" + _REQUIREMENT
    monkeypatch.setattr(compact_module, "_summarize", lambda *args, **kwargs: _SUMMARY_TEXT)
    befores, candidates = [], []
    original_before = recovery._full_request_tokens
    original_project = recovery._project_mixed_recovery_material

    def full_request_tokens(*args):
        value = original_before(*args)
        befores.append(value)
        return value

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


def _finish_turn(case, request_id: str) -> None:
    """按 Gateway 终态收尾的同一结构化调用释放该请求钉住的执行车道（测试直接调 _run_gateway_ask，不经请求终态）。"""
    case.agent.conversation_store.claims.finish({
        "thread_id": case.thread_id, "expected_task_id": f"gateway:{request_id}", "recover_same_task_only": True,
        "status": "finished", "runtime_facts": {"execution_source": "gateway", "request_id": request_id},
    })


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


def _durable_calibration(case):
    from agent_py_agent.agent.conversation.compact_calibration import CompactRequestCalibration

    observation = case.agent.conversation_store.threads.require(case.thread_id).provider_context_observation
    return CompactRequestCalibration(
        raw_estimated_tokens=observation["raw_estimated_tokens"],
        provider_input_tokens=observation["provider_input_tokens"],
        context_surface_fingerprint=observation["context_surface_fingerprint"],
        compact_generation=observation["compact_generation"], calibration_scope="durable_thread",
    )


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


@pytest.mark.parametrize("scenario", ["calibrated_fit", "calibrated_too_large", "no_observation"])
def test_gateway_recovery_measures_candidates_with_the_preflight_calibration(tmp_path, monkeypatch, scenario):
    case = _gateway_case(tmp_path, monkeypatch, ratio=None if scenario == "no_observation" else _RATIO)
    calibration = _first_turn(case, observed=scenario != "no_observation")
    # 第二回合：大段新需求让完整请求越过上限；候选原始计量也越过上限，只有按观测折算后才可能放下。
    # 探针实测（2026-09-28，窗口 90K/输出 10K）：6_500 段 → 压缩前原始 ≈ 104K、折算 ≈ 85K，候选原始 ≈ 92K、折算 ≈ 73K；
    # 11_500 段 → 候选折算 ≈ 103K 仍超上限。两边都离 80K 上限有几千 tokens 余量。
    sink = _ProgressSink()
    context = _second_turn(case, "材料核对项。" * (11_500 if scenario == "calibrated_too_large" else 6_500), sink)
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
