"""本次采用的 Compact 视图在工具、摘要和原生历史中的定向回归。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service
from agent_py_agent.agent.agent_core.runtime.loop_models import (
    RuntimeLoopParams,
    RuntimeToolLoopSeed,
)
from agent_py_agent.agent.agent_core.runtime.loop_support import (
    _native_ir_with_applied_summary,
    _native_provider_history_messages,
    _tool_loop_execute_params,
)
from agent_py_agent.agent.backends.tool_ir import CompactionSummary, UserTurn
from agent_py_agent.agent.conversation import active_turn_compact, live_tool_compact
from agent_py_agent.agent.conversation.active_turn_compact import (
    ActiveTurnArchiveCompactRequest,
    model_visible_active_turn_tool_calls,
)
from agent_py_agent.agent.conversation.authority import (
    AGENT_THREAD_ID_ATTR,
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from agent_py_agent.agent.conversation.compact_scope import CompactScope
from agent_py_agent.agent.conversation.compact_summary_view import (
    AppliedCompactContext,
    CompactSummaryView,
)
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)


def _context(*, summary: str = "本轮局部摘要") -> AppliedCompactContext:
    return AppliedCompactContext(
        thread_id="thread-1",
        scope=CompactScope(kind="turn", task_id="task-1", turn_id="user-turn-1"),
        view=CompactSummaryView(
            checkpoint_id="checkpoint-1",
            summary=summary,
            generation=1,
            source_tool_refs=({
                "run_id": "run-1", "attempt_id": "attempt-1",
                "turn_id": "model-turn-1", "call_id": "reused-call",
            },),
        ),
    )


def test_explicit_view_hides_only_its_exact_tool_source() -> None:
    attrs = {
        AGENT_THREAD_ID_ATTR: "thread-1",
        CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
    }
    first = {"run_id": "run-1", "attempt_id": "attempt-1", "turn_id": "model-turn-1", "call_id": "reused-call"}
    second = dict(first, attempt_id="attempt-2")
    unknown = {"run_id": "run-1", "call_id": "reused-call"}
    # 显式视图不能触发最新全线程读取；缺失存储也足以证明该分支只读载体。
    visible = model_visible_active_turn_tool_calls(
        SimpleNamespace(conversation_store=None), attrs, [first, second, unknown],
        compact_context=_context(),
    )
    assert visible == [second, unknown]
    for records in ([first], []):
        with pytest.raises(OSError, match="线程不匹配"):
            model_visible_active_turn_tool_calls(
                SimpleNamespace(), {**attrs, AGENT_THREAD_ID_ATTR: "another-thread"},
                records, compact_context=_context(),
            )


def test_applied_context_detaches_mutable_source_refs() -> None:
    original = {"run_id": "run-1", "attempt_id": "attempt-1", "turn_id": "turn-1", "call_id": "call-1"}
    evidence = {"status": {"value": "before"}}
    context = AppliedCompactContext(
        thread_id="thread-1", scope=CompactScope(),
        view=CompactSummaryView(source_tool_refs=(original,), operation_evidence=evidence),
    )
    original["attempt_id"] = "attempt-2"
    evidence["status"]["value"] = "after"
    assert context.view.source_tool_refs[0]["attempt_id"] == "attempt-1"
    assert context.view.operation_evidence["status"]["value"] == "before"


def test_explicit_coverage_without_summary_cannot_hide_source() -> None:
    context = _context(summary="")
    attrs = {
        AGENT_THREAD_ID_ATTR: "thread-1",
        CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
    }
    with pytest.raises(OSError, match="适用摘要缺失"):
        model_visible_active_turn_tool_calls(
            SimpleNamespace(), attrs, [dict(context.view.source_tool_refs[0])],
            compact_context=context,
        )


def test_narrow_seed_injects_summary_without_replacing_handoff() -> None:
    current = UserTurn("当前任务")
    handoff = CompactionSummary("[native-carried-tool-handoff.v1] 原交接")
    media = UserTurn("媒体输入")
    history = [current, handoff, media]
    updated = _native_ir_with_applied_summary(
        history, compact_context=_context(), has_history_seed=False,
    )
    assert updated[0] is current
    assert isinstance(updated[1], CompactionSummary)
    assert "本轮局部摘要" in updated[1].text
    assert updated[2] is handoff
    assert updated[3] is media
    assert _native_ir_with_applied_summary(
        [current, handoff, media], compact_context=_context(), has_history_seed=True,
    ) == [current, handoff, media]


def test_seed_summary_uses_explicit_view_and_preserves_media_message() -> None:
    media = {"role": "user", "content": [{"type": "image", "source": {"data": "media"}}]}
    seed = SimpleNamespace(
        compact_summary="错误的全线程摘要", compact_generation=9,
        canonical_messages=(media,),
    )
    messages = _native_provider_history_messages(
        SimpleNamespace(conversation_history_seed=seed, compact_context=_context())
    )
    rendered = json.dumps(messages, ensure_ascii=False)
    assert "本轮局部摘要" in rendered
    assert "错误的全线程摘要" not in rendered
    assert messages[-1] == media


def test_native_loop_rebuild_injects_applied_summary_with_narrow_seed() -> None:
    context = _context()
    runtime_params = RuntimeLoopParams(
        user_prompt="继续当前任务", root_user_prompt="继续当前任务",
        memories=[], runtime_injections=[], routed_context=None,
        resume_context_section="", conversation_history_seed=None,
        compact_context=context,
        task_attributes={
            AGENT_THREAD_ID_ATTR: "thread-1",
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
        },
    )
    seed = RuntimeToolLoopSeed(
        params=runtime_params, memories=[], tool_catalog_section="",
        tool_recommendations_section="",
        tool_runtime_snapshot=runtime_snapshot_for_model_specs((), run_id="test-run"),
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="test-run", source_protocol="native"),
    )
    params = _tool_loop_execute_params(SimpleNamespace(), seed)
    assert params.compact_context is context
    assert params.provider_history_messages == []
    assert len(params.tool_ir_history) == 2
    assert isinstance(params.tool_ir_history[1], CompactionSummary)
    assert "本轮局部摘要" in params.tool_ir_history[1].text


def test_native_loop_with_seed_uses_one_view_summary_and_keeps_media() -> None:
    context = _context()
    media = {"role": "user", "content": [{"type": "image", "source": {"data": "media"}}]}
    history_seed = SimpleNamespace(
        compact_summary="错误的全线程摘要", compact_generation=9,
        canonical_messages=(media,),
    )
    runtime_params = RuntimeLoopParams(
        user_prompt="继续当前任务", root_user_prompt="继续当前任务",
        memories=[], runtime_injections=[], routed_context=None,
        resume_context_section="", conversation_history_seed=history_seed,
        compact_context=context,
        task_attributes={
            AGENT_THREAD_ID_ATTR: "thread-1",
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
        },
    )
    seed = RuntimeToolLoopSeed(
        params=runtime_params, memories=[], tool_catalog_section="",
        tool_recommendations_section="",
        tool_runtime_snapshot=runtime_snapshot_for_model_specs((), run_id="test-run"),
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="test-run", source_protocol="native"),
    )
    params = _tool_loop_execute_params(SimpleNamespace(), seed)
    assert not any(isinstance(item, CompactionSummary) for item in params.tool_ir_history)
    assert params.provider_history_messages[-1] == media
    rendered = json.dumps(params.provider_history_messages, ensure_ascii=False)
    assert rendered.count("本轮局部摘要") == 1
    assert "错误的全线程摘要" not in rendered


def test_text_loop_rebuild_shows_summary_for_hidden_archive() -> None:
    context = _context()
    covered = {
        "run_id": "run-1", "attempt_id": "attempt-1", "turn_id": "model-turn-1",
        "call_id": "reused-call", "tool": "shell", "ok": True,
    }
    runtime_params = RuntimeLoopParams(
        user_prompt="继续当前任务", root_user_prompt="继续当前任务",
        memories=[], runtime_injections=[], routed_context=None,
        resume_context_section="", conversation_history_seed=None,
        compact_context=context, carried_archive_tool_calls=[covered],
        task_attributes={
            AGENT_THREAD_ID_ATTR: "thread-1",
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
        },
    )
    seed = RuntimeToolLoopSeed(
        params=runtime_params, memories=[], tool_catalog_section="",
        tool_recommendations_section="",
        tool_runtime_snapshot=runtime_snapshot_for_model_specs((), run_id="test-run"),
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="test-run", source_protocol="text"),
    )
    params = _tool_loop_execute_params(SimpleNamespace(), seed)
    assert params.archive_tool_calls == [covered]
    assert params.tool_ir_history == []
    assert "本轮局部摘要" in params.tool_context[0]
    assert all("reused-call" not in item for item in params.tool_context)


def test_text_history_seed_uses_applied_summary_and_keeps_messages() -> None:
    seed = SimpleNamespace(
        compact_summary="错误的全线程摘要", compact_generation=9,
        messages=(("user", "原历史消息"),),
    )
    rendered = _tool_loop_service._text_conversation_history_section(
        seed, compact_context=_context(),
    )
    assert "本轮局部摘要" in rendered
    assert "错误的全线程摘要" not in rendered
    assert "原历史消息" in rendered


def test_active_turn_summary_uses_applied_view_base(monkeypatch) -> None:
    from agent_py_agent.agent.memory_archive import compact_semantic_summary
    monkeypatch.setattr(
        compact_semantic_summary, "semantic_summary_config",
        lambda _agent: SimpleNamespace(enabled=True, max_input_chars=1000),
    )
    requests = []

    def summarize(request):
        requests.append(request)
        return "下一轮摘要"

    monkeypatch.setattr(compact_semantic_summary, "summarize_live_tool_history", summarize)
    result = active_turn_compact._active_turn_replacement_summary(
        SimpleNamespace(backend=None),
        SimpleNamespace(
            thread=SimpleNamespace(workspace_task_id="task-1", summary="错误的全线程摘要"),
            source_records=({
                "run_id": "run-1", "attempt_id": "attempt-1", "turn_id": "model-turn-2", "call_id": "new-call",
                "tool": "read_file", "ok": True, "model_summary": "本次完整工具材料",
                "model_parameters": {"path": "source.txt"},
            },),
        ),
        ActiveTurnArchiveCompactRequest(
            task_attributes={}, request_id="request-1", attempt_id="attempt-1",
            compact_context=_context(),
        ),
    )
    assert result == "下一轮摘要"
    assert requests[0].previous_summary == "本轮局部摘要"
    assert "本次完整工具材料" in requests[0].history[0].text
    assert "model-turn-2" in requests[0].history[0].text


def test_live_binding_and_writer_keep_applied_scope_and_base(monkeypatch) -> None:
    context = _context()
    thread = SimpleNamespace(thread_id="thread-1")
    store = SimpleNamespace(threads=SimpleNamespace(load_report=lambda _id: (thread, None)))
    agent = SimpleNamespace(conversation_store=store)
    monkeypatch.setattr(live_tool_compact, "compact_circuit_is_open", lambda *_args, **_kwargs: False)
    binding = live_tool_compact.resolve_live_tool_compact_binding(
        agent,
        task_attributes={
            AGENT_THREAD_ID_ATTR: "thread-1",
            CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True,
        },
        policy=SimpleNamespace(allow_persistent_apply=True),
        compact_context=context,
    )
    assert binding is not None and binding.scope == context.scope

    class Captured(Exception):
        pass

    written = []

    def capture(_agent, request):
        written.append(request)
        raise Captured

    monkeypatch.setattr(live_tool_compact, "write_live_tool_compact_checkpoint", capture)
    ref = {"run_id": "run-1", "attempt_id": "attempt-2", "turn_id": "model-turn-2", "call_id": "call-2"}
    with pytest.raises(Captured):
        live_tool_compact.commit_live_tool_compact(
            agent, binding,
            live_tool_compact.LiveToolCompactCommitRequest(
                summary="新摘要", source_tool_call_ids=("call-2",),
                retained_tool_call_ids=(), projected_tokens_before=100,
                projected_tokens_after=50, policy=SimpleNamespace(),
                request_id="request-1", attempt_id="attempt-2", source_tool_refs=(ref,),
                summary_base_checkpoint_id=context.view.checkpoint_id,
            ),
        )
    assert written[0].scope == context.scope
    assert written[0].summary_base_checkpoint_id == "checkpoint-1"


def test_native_success_refreshes_only_run_local_view() -> None:
    previous = _context()
    media = {"role": "user", "content": [{"type": "image", "source": {"data": "media"}}]}
    seed = SimpleNamespace(
        compact_summary="旧摘要", compact_generation=1, canonical_messages=(media,),
    )
    params = SimpleNamespace(
        compact_context=previous, conversation_history_seed=seed,
        provider_history_messages=_native_provider_history_messages(SimpleNamespace(
            compact_context=previous, conversation_history_seed=seed,
        )),
        tool_ir_history=[CompactionSummary("下一轮摘要")],
    )
    ref = {"run_id": "run-1", "attempt_id": "attempt-2", "turn_id": "model-turn-2", "call_id": "call-2"}
    _tool_loop_service._refresh_native_compact_context(
        params,
        SimpleNamespace(compact_checkpoint_id="checkpoint-2", compact_generation=2),
        summary="下一轮摘要", source_refs=(ref,),
    )
    assert params.compact_context.scope == previous.scope
    assert params.compact_context.view.checkpoint_id == "checkpoint-2"
    assert params.compact_context.view.summary == "下一轮摘要"
    assert params.compact_context.view.source_tool_refs == (*previous.view.source_tool_refs, ref)
    assert previous.view.checkpoint_id == "checkpoint-1"
    assert params.provider_history_messages == [media]
    assert params.tool_ir_history == [CompactionSummary("下一轮摘要")]


def test_native_commit_forwards_base_and_refreshes_context(monkeypatch) -> None:
    context = _context()
    params = SimpleNamespace(
        compact_context=context, request_id="request-1", attempt_id="attempt-2",
    )
    ref = {"run_id": "run-1", "attempt_id": "attempt-2", "turn_id": "model-turn-2", "call_id": "call-2"}
    plan = SimpleNamespace(
        binding=object(), before_tool_refs=(ref,), semantic_summary="下一轮摘要",
        before_tokens=100, policy=SimpleNamespace(), forced=False,
    )
    monkeypatch.setattr(_tool_loop_service, "_native_tool_refs", lambda _params: ())
    monkeypatch.setattr(_tool_loop_service, "_native_compact_interrupted", lambda _params: False)
    captured = []

    def commit(_agent, _binding, request):
        captured.append(request)
        return SimpleNamespace(compact_checkpoint_id="checkpoint-2", compact_generation=2)

    monkeypatch.setattr(live_tool_compact, "commit_live_tool_compact", commit)
    generation = _tool_loop_service._commit_native_ir_generation(
        SimpleNamespace(), params, plan, after_tokens=50,
    )
    assert generation == 2
    assert captured[0].summary_base_checkpoint_id == "checkpoint-1"
    assert captured[0].source_tool_refs == (ref,)
    assert params.compact_context.scope == context.scope
    assert params.compact_context.view.checkpoint_id == "checkpoint-2"
    assert params.compact_context.view.source_tool_refs == (*context.view.source_tool_refs, ref)
