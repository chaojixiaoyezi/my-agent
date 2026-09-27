"""“候选过大”压缩失败必须留下容量计量（2026-09-27）。

背景：G2 复验里第二代自动 Compact 报 COMPACT_CANDIDATE_TOO_LARGE，失败进度只有 after_tokens=0（未计量默认值），
候选总量、输入上限和摘要占比都没有留下，无法判断是摘要过长还是固定开销过大。
锁定：会话 transcript 与活动回合两条压缩链在候选被输入上限拒掉时，错误带 CompactCapacityFacts，failed 进度经公开白名单
带出 candidate_tokens / input_ceiling_tokens / summary_tokens / retained_items / candidates_tried；TUI 失败行显示候选与上限。
只替身摘要模型与计量值，分区、接受门、熔断记账和进度发射都走生产链。
"""

from __future__ import annotations

from dataclasses import asdict, fields

import pytest

from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.http import HttpBackend
from agent_py_agent.agent.conversation import compact as compact_module
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
    facts = CompactCapacityFacts(7_000, 6_000, 1_200, 4, 2)
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
        "retained_items": -3, "candidates_tried": None, "context_window_tokens": 65_536,
    })
    # 同一行复制的模型窗口（原有可选字段）也只在给出时出现。
    assert value["context_window_tokens"] == 65_536
    assert {key: value[key] for key in COMPACT_CAPACITY_PROGRESS_FIELDS} == {
        "candidate_tokens": 52_100, "input_ceiling_tokens": 49_152, "summary_tokens": 9_800,
        "retained_items": 0, "candidates_tried": 0,
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
        retained_items=retained, candidates_tried=2,
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
        "retained_items": 3, "candidates_tried": 2,
    }))
    rendered = _rendered(store)
    assert "COMPACT_CANDIDATE_TOO_LARGE · 候选 52,100 / 上限 49,152 tokens（摘要约 9,800）" in rendered


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
