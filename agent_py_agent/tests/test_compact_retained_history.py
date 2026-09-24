"""Compact来源与保留尾部必须完整进入模型，普通展示窗口保持原行为。"""
from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import compact_request_recovery
from agent_py_agent.agent.conversation import ConversationStore, background_execution
from agent_py_agent.agent.conversation.agent_thread import _agent_thread_history_seed
from agent_py_agent.agent.conversation.background_context import TaskScopeDecision
from agent_py_agent.agent.conversation.background_history_seed import (
    BackgroundHistoryProjection,
    project_background_history_seed,
)
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactView
from agent_py_agent.agent.conversation.history_projection import conversation_history_rows
from agent_py_agent.agent.conversation.history_seed import (
    seed_provider_history_messages,
    seed_text_messages,
)
from agent_py_agent.agent.conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
)
from agent_py_agent.agent.gateway_parts import request_context, request_execution, request_prompt
from agent_py_agent.agent.settings import model_profiles
from agent_py_agent.tests.test_background_compact_recovery import _background
from agent_py_agent.tests.test_gateway_model_adoption import actual_request
from agent_py_agent.tests.test_model_profiles import add
from agent_py_agent.tests.test_subagent_compact_recovery import _child, _http

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="


# LLM: 只写测试临时canonical store，最早回合带真实PNG字节与工具配对；尾行足够大才能暴露二次窗口删行。
# 函数用途: 构造带原生媒体、工具结果和完整首中尾文字的已结束历史，不改模型准备或容量估算器。
def _append_history(store, tid, *, size):
    media_text = "EARLIEST-MEDIA-TURN"
    native = [
        {"role": "user", "content": [{"type": "text", "text": media_text},
         {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "retained-call", "name": "read_file", "input": {"path": "evidence.txt"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "retained-call", "content": "RETAINED-TOOL-RESULT"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "MEDIA-ANSWER"}]},
    ]
    rows = []
    for index in range(4):
        content = (media_text if index == 0 else f"ROW-{index}-HEAD-" + "a" * size + f"-ROW-{index}-TAIL")
        metadata = {"conversation_request_id": f"retained-turn-{index}"}
        if index == 0:
            metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY] = canonical_native_messages_envelope(native)
        rows.append(store.messages.append({"thread_id": tid, "role": "user" if index % 2 == 0 else "assistant",
                                           "content": content, "metadata": metadata}))
    return tuple(rows)


@pytest.mark.parametrize("host", ["gateway", "background", "child"])
def test_preselected_seed_keeps_all_rows_and_native_envelope(tmp_path, monkeypatch, host):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner"})
    rows = _append_history(store, thread.thread_id, size=800)
    agent = SimpleNamespace(conversation_store=store, config=SimpleNamespace(conversation_history_max_chars=1000))
    ordinary = conversation_history_rows(agent, thread.thread_id, "", [], rows=rows, token_budget=1)
    assert len(ordinary) < len(rows), "普通展示仍应应用原窗口"
    if host == "child":
        view = ConversationCompactView(thread.thread_id, 1, "已读前缀", rows, {}, {}, 1, True)
        seed = _agent_thread_history_seed(agent, view)
    elif host == "background":
        projection = BackgroundHistoryProjection(thread.thread_id, TaskScopeDecision("", frozenset(), None, False), {}, 1)
        seed = project_background_history_seed(agent, projection, rows, scope_applied=True)
    else:
        source = request_context._gateway_history_source(rows, "", None)
        seed = request_prompt.gateway_conversation_history_seed(request_context.GatewayConversationContext(
            thread_id=thread.thread_id, history_source=source,
        ))
    # 种子只带只读来源；完整内容在原 native/text 准备边界解析，不能丢行或媒体。
    assert seed.source is not None and not seed.messages and not seed.canonical_messages
    assert seed_text_messages(seed) == tuple((row.role, row.content) for row in rows)
    encoded = json.dumps(seed_provider_history_messages(seed), ensure_ascii=False)
    assert "EARLIEST-MEDIA-TURN" in encoded and "RETAINED-TOOL-RESULT" in encoded
    assert PNG in encoded and "retained-call" in encoded
    for row in rows[1:]:
        assert row.content in encoded


def test_complete_projection_requires_explicit_source():
    with pytest.raises(ValueError, match="explicit source"):
        conversation_history_rows(SimpleNamespace(), "thread", "", [], preserve_complete=True)


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("host", ["gateway", "background", "child"])
@pytest.mark.parametrize("backend", ["anthropic_compatible", "openai_compatible"])
def test_retained_media_reaches_wire_or_fails_capacity_without_dropping(tmp_path, monkeypatch, host, backend, large):
    if host == "gateway":
        fixture = actual_request(tmp_path, mode="disabled", tools=False, original_window=200_000)
        agent, tid = fixture.agent, fixture.thread_id
        if backend == "openai_compatible":
            profile, _ = add(agent, model_name="original-openai", model_backend=backend, model_context_window_tokens=200_000)
            model_profiles.execute_model_profile_operation(agent, "select", {"profile_id": profile}, thread_id=tid)
        run = partial(request_execution._run_gateway_ask, fixture.context)
    elif host == "child":
        agent, task = _child(tmp_path, backend=backend, tools=False)
        tid = task.agent_thread_id
        run = partial(agent.run_subagent, task.id, dry_run=False, probe=False)
    else:
        agent, store, thread, request, execution, sink = _background(tmp_path, backend=backend, detached=False, with_history=False)
        tid = thread.thread_id
        run = partial(background_execution.run_background_turn_with_compact,
            execution, thread, request, user_prompt="继续核对旧资料", continuation_injection=[],
            proactive_delivery_available=False, activity_sink=sink,
        )
    rows = _append_history(agent.conversation_store, tid, size=200_000 if large else 800)
    if large:
        assert sum(len(row.content) for row in rows) > 200_000 * .9 * 3
    captured, recoveries = [], []
    original_select = compact_request_recovery.PreparedCompactRecovery.select

    def select(recovery, *args):
        recoveries.append(recovery)
        return original_select(recovery, *args)

    monkeypatch.setattr(compact_request_recovery.PreparedCompactRecovery, "select", select)
    original_capture = compact_request_recovery.capture_tool_loop_request

    def capture(*args, **kwargs):
        frozen = original_capture(*args, **kwargs)
        captured.append(frozen)
        return frozen

    monkeypatch.setattr(compact_request_recovery, "capture_tool_loop_request", capture)

    def inspect(wire, number):
        assert number == 1, "未知媒体容量应保留原历史发送，不能先丢媒体再发摘要"
        encoded = json.dumps(wire["messages"], ensure_ascii=False)
        assert "EARLIEST-MEDIA-TURN" in encoded and "RETAINED-TOOL-RESULT" in encoded
        assert PNG in encoded and "retained-call" in encoded
        for row in rows[1:]:
            assert row.content in encoded

    calls, _ = _http(monkeypatch, backend=backend, on_business=inspect)
    if large and host != "child":
        with pytest.raises((ConversationCompactError, RuntimeError)) as failure:
            run()
        assert getattr(failure.value, "error_code", None) == "COMPACT_REQUEST_PROJECTION_UNKNOWN"
    else:
        result = run()
        if host == "child":
            assert result.ok is not large
        else:
            assert result.runtime_status != "context_overflow"
    assert len(calls) == (0 if large else 1)
    assert captured
    for frozen in captured:
        encoded = json.dumps(frozen.provider_history_messages, ensure_ascii=False)
        assert "EARLIEST-MEDIA-TURN" in encoded and PNG in encoded
        for row in rows[1:]:
            assert row.content in encoded
    if not large:
        recovery = recoveries[0]
        agent.config.conversation_history_max_chars = 1000
        view = ConversationCompactView(tid, 1, "候选只总结更早的已读文字", rows, {}, {}, 1, True)
        material = recovery.project_candidate(recovery.render_params, captured[0], view)
        assert seed_text_messages(material.params.conversation_history_seed) == tuple((row.role, row.content) for row in rows)
        assert material.projection.status == "ready"
        projected = json.dumps(material.projection.messages, ensure_ascii=False)
        assert "EARLIEST-MEDIA-TURN" in projected and PNG in projected
        for row in rows[1:]:
            assert row.content in projected
        assert not recovery.committed, "纯候选投影不能取得摘要覆盖或修改原检查点"
    assert agent.conversation_store.threads.require(tid).compact_generation == 0


@pytest.mark.parametrize("include_transcript", [False, True])
def test_child_context_reads_transcript_only_when_rendered(tmp_path, include_transcript):
    from collections.abc import Sequence

    from agent_py_agent.agent.conversation.agent_thread import _render_agent_thread_context
    from agent_py_agent.agent.conversation.models import MessageLogEntry

    reads = []

    class ObservedRows(Sequence):
        def __len__(self):
            return 1

        def __getitem__(self, index):
            if index != 0:
                raise IndexError(index)
            reads.append(index)
            return MessageLogEntry(message_id="m1", thread_id="thread", role="user", content="完整历史原文")

    view = ConversationCompactView("thread", 3, "已有摘要", ObservedRows(), {"verified": True}, {}, 1000, False)
    rendered = _render_agent_thread_context(SimpleNamespace(), view, include_transcript=include_transcript)
    assert "thread" in rendered and '"verified":true' in rendered
    assert ("完整历史原文" in rendered) is include_transcript
    assert ("已有摘要" in rendered) is include_transcript
    assert bool(reads) is include_transcript
