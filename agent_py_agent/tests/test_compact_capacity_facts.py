"""“候选过大”压缩失败必须留下容量计量（2026-09-27）。

背景：G2 复验里第二代自动 Compact 报 COMPACT_CANDIDATE_TOO_LARGE，失败进度只有 after_tokens=0（未计量默认值），
候选总量、输入上限和摘要占比都没有留下，无法判断是摘要过长还是固定开销过大。
锁定：会话 transcript 与活动回合两条压缩链在候选被输入上限拒掉时，错误带 CompactCapacityFacts，failed 进度经公开白名单
带出 candidate_tokens / input_ceiling_tokens / summary_tokens / fixed_tokens / retained_items / retained_ir_items /
retained_ir_tokens / candidates_tried；TUI 失败行显示候选、上限与实测固定开销。
固定开销只在真正抛出 COMPACT_CANDIDATE_TOO_LARGE 时实测一次：走同一投影器的“只计量、不提交”入口（measure_only 视图，
空摘要、无保留；宿主投影器缺失时走同一本地估算，不用两个估算相减），测不出时字段缺失（None），不写 0。
保留 IR 按候选实际要发送的材料计，与只统计工具/会话保留的 retained_items 分开。
只替身摘要模型与计量值，分区、接受门、熔断记账和进度发射都走生产链。
"""

from __future__ import annotations

from dataclasses import asdict, fields
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.http import HttpBackend
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn
from agent_py_agent.agent.conversation import compact as compact_module
from agent_py_agent.agent.conversation.active_turn_compact import CarriedToolCompactSource
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_guard import (
    CompactCapacityFacts,
    ConversationCompactError,
    compact_failure_progress_fields,
)
from agent_py_agent.agent.conversation.compact_progress import (
    COMPACT_CAPACITY_PROGRESS_FIELDS,
    normalize_conversation_compact_progress,
)
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
from agent_py_agent.agent.conversation.compact_tool_summary import retained_ir_facts
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext, render_tui_snapshot
from agent_py_agent.cli.chat_parts.tui_events import TuiEventSequencer
from agent_py_agent.cli.chat_parts.tui_markdown import fragments_text
from agent_py_agent.cli.chat_parts.tui_view_model import TuiStateStore

_WINDOW = 10_000
_OUTPUT_CAP = 4_000
# 输入上限 = min(触发线 90%×10000=9000, 窗口 10000 − 输出预留 4000) = 6000。
_CEILING = 6_000


def _transcript_case(tmp_path, monkeypatch, turns: int):
    config = AgentConfig(
        model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
        max_tokens=64, model_context_window_tokens=_WINDOW, memory_compact_auto_trigger_percent=90,
        memory_compact_recovery_target_percent=60,
    )
    config.config_sources = {"model_context_window_tokens": {"source": "test"}}
    agent = SimpleAgent(config, tmp_path)
    agent.backend = HttpBackend(BackendOptions(
        api_base="https://example.invalid", api_key="fake-key", model_name="test-model",
        context_window_tokens=_WINDOW, max_tokens=_OUTPUT_CAP,
    ))
    store = agent.conversation_store
    thread = store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "capacity-facts"})
    for index in range(turns):
        for role in ("user", "assistant"):
            store.messages.append({"thread_id": thread.thread_id, "role": role, "content": f"完整旧轮次 {index}",
                                   "metadata": {"conversation_request_id": f"prior-turn-{index}"}})
    return agent, store, thread


def _rendered(store: TuiStateStore) -> str:
    frame = render_tui_snapshot(store.snapshot(), TuiRenderContext(width=160))
    return "\n".join(fragments_text(line) for line in frame.transcript_lines)


def _failed_event(events):
    failed = [event for event in events if event.get("phase") == "failed"]
    assert len(failed) == 1, events
    return failed[0]


def test_capacity_fact_fields_match_the_public_progress_whitelist() -> None:
    assert tuple(item.name for item in fields(CompactCapacityFacts)) == COMPACT_CAPACITY_PROGRESS_FIELDS


def test_failure_fields_carry_capacity_only_when_the_error_has_it() -> None:
    facts = CompactCapacityFacts(
        candidate_tokens=7_000, input_ceiling_tokens=6_000, summary_tokens=1_200, fixed_tokens=3_500,
        retained_items=4, retained_ir_items=2, retained_ir_tokens=900, candidates_tried=2,
    )
    with_capacity = ConversationCompactError("too large", code="COMPACT_CANDIDATE_TOO_LARGE", capacity=facts)
    assert compact_failure_progress_fields(with_capacity) == {"error_code": "COMPACT_CANDIDATE_TOO_LARGE", **asdict(facts)}
    plain = ConversationCompactError("empty", code="COMPACT_EMPTY_SUMMARY")
    assert compact_failure_progress_fields(plain) == {"error_code": "COMPACT_EMPTY_SUMMARY"}
    assert compact_failure_progress_fields(RuntimeError("boom")) == {"error_code": "COMPACT_RUNTIMEERROR"}


def test_normalizer_copies_capacity_only_when_given_and_clamps_it() -> None:
    base = {
        "schema": "conversation_compaction_progress.v1", "phase": "failed", "stage": "failed", "percent": 0,
        "generation": 2, "operation_id": "compact:test", "source_kind": "conversation_transcript",
        "commit_authority": "conversation_thread", "error_code": "COMPACT_CANDIDATE_TOO_LARGE",
    }
    plain = normalize_conversation_compact_progress(base)
    assert not {"context_window_tokens", *COMPACT_CAPACITY_PROGRESS_FIELDS} & set(plain)
    value = normalize_conversation_compact_progress({
        **base, "candidate_tokens": 52_100, "input_ceiling_tokens": 49_152, "summary_tokens": "9800",
        "fixed_tokens": 12_000, "retained_items": -3, "retained_ir_items": 2, "candidates_tried": None,
        "context_window_tokens": 65_536,
    })
    # 同一行复制的模型窗口（原有可选字段）也只在给出时出现。
    assert value["context_window_tokens"] == 65_536
    # 没给出的 retained_ir_tokens 不补零、也不从其它字段推断。
    assert "retained_ir_tokens" not in value
    assert {key: value[key] for key in COMPACT_CAPACITY_PROGRESS_FIELDS if key != "retained_ir_tokens"} == {
        "candidate_tokens": 52_100, "input_ceiling_tokens": 49_152, "summary_tokens": 9_800,
        "fixed_tokens": 12_000, "retained_items": 0, "retained_ir_items": 2, "candidates_tried": 0,
    }


@pytest.mark.parametrize("measured", [(7_000, 6_500), (6_500, 7_000)])
def test_transcript_failure_reports_the_smallest_rejected_candidate(tmp_path, monkeypatch, measured) -> None:
    agent, store, thread = _transcript_case(tmp_path, monkeypatch, turns=3)
    # 英文摘要的估算 token 与字符数不同，能区分“摘要 token”与“摘要字符”。
    summaries = iter(("first candidate summary " * 40, "second summary"))
    monkeypatch.setattr(compact_module, "_summarize", lambda *args, **kwargs: next(summaries))
    remaining = list(measured)
    seen = []

    def projected(*args, **_kwargs):
        summary, rows = args[1], args[2]
        if not summary:
            return 8_500
        tokens = remaining.pop(0)
        seen.append((tokens, summary, len(rows)))
        return tokens

    monkeypatch.setattr(compact_module, "_projected_context_tokens", projected)
    events = []
    with pytest.raises(ConversationCompactError) as error:
        prepare_conversation_context(agent, store, thread, options=ConversationCompactOptions(
            current_prompt="继续完整任务", progress_callback=events.append,
        ))
    assert error.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    assert len(seen) == 2 and seen[0][2] > seen[1][2] == 0, seen
    tokens, summary, retained = min(seen)
    assert estimate_tokens(summary) != len(summary)
    expected = CompactCapacityFacts(
        candidate_tokens=tokens, input_ceiling_tokens=_CEILING, summary_tokens=estimate_tokens(summary),
        # 没有宿主投影器时固定开销走同一本地估算路径；本例假实现把空摘要那一版定为 8_500。
        fixed_tokens=8_500, retained_items=retained, retained_ir_items=0, retained_ir_tokens=0,
        candidates_tried=2,
    )
    assert error.value.capacity == expected
    failed = _failed_event(events)
    assert {key: failed[key] for key in COMPACT_CAPACITY_PROGRESS_FIELDS} == asdict(expected)
    assert failed["error_code"] == "COMPACT_CANDIDATE_TOO_LARGE" and failed["after_tokens"] == 0
    public = normalize_conversation_compact_progress(failed)
    assert {key: public[key] for key in COMPACT_CAPACITY_PROGRESS_FIELDS} == asdict(expected)
    assert store.threads.load(thread.thread_id).compact_failure_code == "COMPACT_CANDIDATE_TOO_LARGE"


def test_transcript_failure_without_candidate_keeps_capacity_empty(tmp_path, monkeypatch) -> None:
    agent, store, thread = _transcript_case(tmp_path, monkeypatch, turns=2)

    def broken(*args, **kwargs):
        raise ConversationCompactError("摘要为空", code="COMPACT_EMPTY_SUMMARY")

    monkeypatch.setattr(compact_module, "_summarize", broken)
    events = []
    with pytest.raises(ConversationCompactError) as error:
        prepare_conversation_context(agent, store, thread, options=ConversationCompactOptions(
            current_prompt="继续完整任务", force=True, progress_callback=events.append,
        ))
    assert error.value.code == "COMPACT_EMPTY_SUMMARY" and error.value.capacity is None
    failed = _failed_event(events)
    assert failed["error_code"] == "COMPACT_EMPTY_SUMMARY"
    assert not set(COMPACT_CAPACITY_PROGRESS_FIELDS) & set(failed)


def test_accepted_candidate_emits_no_capacity_fields(tmp_path, monkeypatch) -> None:
    agent, store, thread = _transcript_case(tmp_path, monkeypatch, turns=3)
    monkeypatch.setattr(compact_module, "_summarize", lambda *args, **kwargs: "新摘要")
    measured = iter((7_000, 5_000))
    monkeypatch.setattr(compact_module, "_projected_context_tokens",
                        lambda _agent, summary, *args, **kwargs: next(measured) if summary else 8_500)
    events = []
    result = prepare_conversation_context(agent, store, thread, options=ConversationCompactOptions(
        current_prompt="继续完整任务", progress_callback=events.append,
    ))
    assert result.compacted and result.projected_tokens == 5_000
    assert all(not set(COMPACT_CAPACITY_PROGRESS_FIELDS) & set(event) for event in events)


def test_tui_failure_line_shows_candidate_ceiling_and_summary_share() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("compact-capacity", clock=lambda: 12.0)
    store.publish(seq.emit("conversation_compaction_started", "started", "compact:req:capacity",
                           {"generation": 2, "percent": 5, "stage": "preparing"}))
    store.publish(seq.emit("conversation_compaction_failed", "failed", "compact:req:capacity", {
        "generation": 2, "percent": 0, "stage": "failed", "error_code": "COMPACT_CANDIDATE_TOO_LARGE",
        "candidate_tokens": 52_100, "input_ceiling_tokens": 49_152, "summary_tokens": 9_800,
        "fixed_tokens": 12_000, "retained_items": 3, "retained_ir_items": 2, "retained_ir_tokens": 4_500,
        "candidates_tried": 2,
    }))
    rendered = _rendered(store)
    assert (
        "COMPACT_CANDIDATE_TOO_LARGE · 候选 52,100 / 上限 49,152 tokens（摘要约 9,800） · 固定开销约 12,000"
        in rendered
    )


def test_tui_failure_line_without_capacity_keeps_the_old_text() -> None:
    store = TuiStateStore()
    seq = TuiEventSequencer("compact-capacity-plain", clock=lambda: 12.0)
    store.publish(seq.emit("conversation_compaction_started", "started", "compact:req:plain",
                           {"generation": 1, "percent": 5, "stage": "preparing"}))
    store.publish(seq.emit("conversation_compaction_failed", "failed", "compact:req:plain", {
        "generation": 1, "percent": 0, "stage": "failed", "error_code": "COMPACT_EMPTY_SUMMARY",
        "candidate_tokens": 52_100,
    }))
    rendered = _rendered(store)
    assert "上下文压缩失败，原上下文已保留 · COMPACT_EMPTY_SUMMARY" in rendered
    assert "候选" not in rendered and "上限" not in rendered


# LLM: 宿主的完整投影器同时供候选与固定开销使用；固定开销那一版必须是空摘要、无任何保留，且只在失败路径多投一次。
# 函数用途: 端到端验证 transcript 失败留下的 fixed_tokens 是实测投影，而不是两个估算相减。
def test_transcript_failure_measures_fixed_overhead_with_the_host_projector(tmp_path, monkeypatch) -> None:
    agent, store, thread = _transcript_case(tmp_path, monkeypatch, turns=3)
    monkeypatch.setattr(compact_module, "_summarize", lambda *args, **kwargs: "候选摘要")
    views = []

    def projector(view):
        views.append(view)
        # 初始投影（is_candidate=False）也必须超上限，否则根本不会进入压缩链。
        if not view.is_candidate:
            return ConversationCompactProjection(7_200, {"kind": "initial"})
        return ConversationCompactProjection(
            3_100 if not view.summary else 7_200,
            {"summary": view.summary, "retained": len(view.messages)},
        )

    events = []
    with pytest.raises(ConversationCompactError) as error:
        prepare_conversation_context(agent, store, thread, options=ConversationCompactOptions(
            current_prompt="继续完整任务", request_projector=projector, progress_callback=events.append,
        ))
    assert error.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    capacity = error.value.capacity
    assert capacity.candidate_tokens == 7_200 and capacity.input_ceiling_tokens == _CEILING
    assert capacity.fixed_tokens == 3_100
    # 本例没有工具来源，保留 IR 计量保持 0，不与固定开销混算。
    assert capacity.retained_ir_items == 0 and capacity.retained_ir_tokens == 0
    candidates = [view for view in views if view.is_candidate and view.summary]
    overhead = [view for view in views if view.is_candidate and not view.summary]
    # 两个分区的候选各投一次，固定开销只多投一次：成功路径不会走到这里。
    assert len(candidates) == 2 and len(overhead) == 1, views
    assert overhead[0].measure_only and not any(view.measure_only for view in candidates)
    assert overhead[0].retained_tool_records == () and overhead[0].retained_ir_history == ()
    assert overhead[0].messages == ()
    failed = _failed_event(events)
    assert {key: failed[key] for key in COMPACT_CAPACITY_PROGRESS_FIELDS} == asdict(capacity)


# LLM: 固定开销测不出时是 None：公开进度字段缺失、TUI 失败行不显示，不写 0；也不能盖住原候选过大失败，
#   不能让失败路径少记其它容量计量。
# 函数用途: 验证固定开销投影不可用时原错误码与候选计量保持不变，失败事件与界面都不出现固定开销。
def test_fixed_overhead_projection_failure_leaves_the_field_missing(tmp_path, monkeypatch) -> None:
    agent, store, thread = _transcript_case(tmp_path, monkeypatch, turns=3)
    monkeypatch.setattr(compact_module, "_summarize", lambda *args, **kwargs: "候选摘要")

    def projector(view):
        if view.measure_only:
            raise RuntimeError("固定开销投影不可用")
        return ConversationCompactProjection(7_200, object())

    events = []
    with pytest.raises(ConversationCompactError) as error:
        prepare_conversation_context(agent, store, thread, options=ConversationCompactOptions(
            current_prompt="继续完整任务", request_projector=projector, progress_callback=events.append,
        ))
    assert error.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    assert error.value.capacity.fixed_tokens is None
    assert error.value.capacity.candidate_tokens == 7_200
    measured = set(COMPACT_CAPACITY_PROGRESS_FIELDS) - {"fixed_tokens"}
    public = normalize_conversation_compact_progress(_failed_event(events))
    assert "fixed_tokens" not in public and measured <= set(public)
    tui = TuiStateStore()
    seq = TuiEventSequencer("compact-capacity-unknown-fixed", clock=lambda: 12.0)
    tui.publish(seq.emit("conversation_compaction_started", "started", "compact:req:unknown-fixed",
                         {"generation": 1, "percent": 5, "stage": "preparing"}))
    tui.publish(seq.emit("conversation_compaction_failed", "failed", "compact:req:unknown-fixed", {
        "generation": 1, "percent": 0, "stage": "failed", "error_code": public["error_code"],
        **{key: public[key] for key in measured},
    }))
    rendered = _rendered(tui)
    assert f"候选 7,200 / 上限 {_CEILING:,} tokens" in rendered and "固定开销" not in rendered


# LLM: 保留 IR 与只统计工具/会话保留的 retained_items 必须分开；固定开销与候选共用同一宿主投影器。
# 函数用途: 直接驱动被拒候选记账，验证固定开销实测值、保留 IR 条数与 token 估算各自独立。
def test_rejected_candidate_records_fixed_overhead_and_retained_ir_separately() -> None:
    record = {"run_id": "run-1", "attempt_id": "attempt-1", "turn_id": "turn-1", "call_id": "call-1",
              "tool": "read_file", "ok": True, "model_parameters": {"path": "evidence.txt"},
              "output_preview": "原始材料"}
    ref = {key: record[key] for key in ("run_id", "attempt_id", "turn_id", "call_id")}
    ir = RuntimeFactsTurn(text="保留的原生事实。" * 60, source="resume")
    source = CarriedToolCompactSource((record,), (), (ref,), (), retained_ir_history=(ir,))
    views = []

    def projector(view):
        views.append(view)
        return ConversationCompactProjection(4_100 if not view.summary else 7_500, {"summary": view.summary})

    request = SimpleNamespace(
        thread=SimpleNamespace(thread_id="thread-1", compact_generation=0),
        policy=SimpleNamespace(trigger_tokens=9_000), current_prompt="继续核对",
        request_projector=projector, tool_source=source, calibration=None,
    )
    candidate = compact_module._CompactCandidate(
        summary="候选摘要", operation_evidence={}, compact_rows=(), retained_tail=(), projected_tokens_after=7_500,
    )
    rejected = compact_module._RejectedCandidates(request)

    assert rejected.reject_if_over(candidate, 6_000) is True
    # 拒绝记账本身不投影固定开销；只有真要抛出失败时的 capacity() 才实测一次。
    assert views == []
    facts = rejected.capacity()
    assert facts.candidate_tokens == 7_500 and facts.input_ceiling_tokens == 6_000
    assert facts.summary_tokens == estimate_tokens("候选摘要")
    assert facts.fixed_tokens == 4_100
    assert (facts.retained_items, facts.retained_ir_items) == (0, 1)
    assert facts.retained_ir_tokens > 0 and facts.candidates_tried == 1
    # 候选投影由生产链的 _measure_candidate 负责；本用例只直接驱动被拒记账，所以固定开销恰好这一次。
    assert len(views) == 1 and views[0].is_candidate and views[0].summary == "" and views[0].measure_only
    assert views[0].messages == () and views[0].retained_tool_records == ()
    assert views[0].retained_ir_history == ()


@pytest.mark.parametrize("ir_history,expected_items", [
    (None, 0), ((), 0), ((RuntimeFactsTurn(text="保留事实。" * 200, source="resume"),), 1),
])
def test_retained_ir_facts_counts_only_non_tool_retained_ir(ir_history, expected_items) -> None:
    items, tokens = retained_ir_facts(ir_history)
    assert items == expected_items
    assert (tokens > 0) is (expected_items > 0)
