"""联合 Compact 的摘要、完整投影、双覆盖和取消必须沿同一真实 Store/CAS。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.common.cancellation import CancellationToken, ToolCancelled
from agent_py_agent.agent.conversation import compact, compact_request_budget
from agent_py_agent.agent.conversation.active_turn_compact import partition_carried_tool_records
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactProviderSurface,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
from agent_py_agent.agent.conversation.compact_tool_summary import carried_compact_source_text
from agent_py_agent.agent.gateway_parts.request_history import append_gateway_conversation_message
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.tests._tool_runtime_harness import canonical_history_call
from agent_py_agent.tests.test_gateway_compact_deferred_source import _pressure_thread


# LLM: 引用必须来自原 ToolCall，重复的裸 call_id 不能替代 run/attempt/turn；正文仅是被摘要的资料。
# 函数用途: 构造带原身份和独有标记的模型可见归档，长正文用于暴露摘要前的中段裁剪。
def _tool_record(index: int, *, large: bool = False) -> dict[str, object]:
    call = canonical_history_call(
        "read_file", {"path": f"evidence-{index}.txt"}, call_id="reused-call",
        run_id="original-run", attempt_id="original-attempt", turn_id=f"original-turn-{index}",
    )
    return {
        **call.to_dict(), "tool": call.tool_name, "ok": True,
        "model_parameters": dict(call.arguments),
        "model_summary": f"ORIGINAL_TOOL_{index:02}_MARKER\n" + (
            "保留的工具原始材料。" * 180 if large else "原始结果"
        ),
    }


# LLM: 只监测真实 CAS，不替换其行为；原 transcript、checkpoint 封印和 reader 全部来自生产实现。
# 函数用途: 建立有两种候选分区的真实历史与工具来源，并记录实际提交次数。
@pytest.fixture
def mixed_case(tmp_path, monkeypatch):
    agent, _, _, context = _pressure_thread(tmp_path)
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.config.memory_compact_recovery_target_percent = 60
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(
            agent, {}, context, request_id=f"short-{role}", role=role, content="应保留的短尾部",
        )
    store = agent.conversation_store
    thread = store.threads.require(context.thread_id)
    source = compact.load_conversation_compact_source(agent, store, thread, scope=THREAD_COMPACT_SCOPE)
    records = [_tool_record(0), {"call_id": "legacy-unknown", "output_preview": "未知身份原文"},
               _tool_record(1), _tool_record(2)]
    case = SimpleNamespace(
        agent=agent, store=store, thread=thread, source=source, records=records, commits=[],
        tool_source=partition_carried_tool_records(records, recent_tail_tokens=10_000),
        raw_rows=tuple(store.messages.recent(thread.thread_id, limit=0)),
        surface=ConversationCompactProviderSurface("固定摘要缓存面", (), "原摘要系统指令"),
        checkpoint_path=agent.home_paths.owner_compact_dir / "conversations" / f"{thread.thread_id}.jsonl",
    )
    original_commit = store.threads.update_compact_state

    def observe_commit(*args, **kwargs):
        case.commits.append((args, kwargs))
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(store.threads, "update_compact_state", observe_commit)
    return case


# LLM: helper 只传递同一个冻结来源和 projector，不另写分区、摘要、计量门或恢复算法。
# 函数用途: 用原公开 Compact 入口执行本例，真实持久写仍由被测代码决定。
def _compact(case, project, *, force: bool = True, **fields):
    return compact.prepare_conversation_context(
        case.agent, case.store, case.thread,
        options=compact.ConversationCompactOptions(
            current_prompt="继续核对原始材料", force=force, source=case.source,
            request_projector=project, provider_surface=case.surface, tool_source=case.tool_source, **fields,
        ),
    )


# LLM: 未提交 checkpoint 可以留在原追加账中，但不得成为链 head、覆盖权或失败取消的新事实。
# 函数用途: 读回真实存储确认没有 CAS，原消息和所有已发布压缩字段保持不变。
def _assert_uncommitted(case, *, failures: int):
    current = case.store.threads.require(case.thread.thread_id)
    assert case.commits == []
    assert current.compact_generation == 0 and current.compact_checkpoint_id == ""
    assert current.summary == "" and current.compacted_through_message_id == ""
    assert current.compacted_through_byte_offset == 0
    assert current.compact_source_messages == current.compact_source_tool_pairs == 0
    assert current.compact_consecutive_failures == failures
    assert committed_compact_checkpoint_chain(case.agent, current) == ()
    assert tuple(case.store.messages.recent(current.thread_id, limit=0)) == case.raw_rows


@pytest.mark.parametrize("response_kind", ["empty", "tool_call"])
def test_mechanical_mixed_summary_keeps_every_tool_source_and_rejects_oversized_request(
    mixed_case, monkeypatch, tmp_path, response_kind,
):
    case = mixed_case
    records = [_tool_record(index, large=True) for index in range(31)]
    records.insert(15, case.records[1])
    case.tool_source = partition_carried_tool_records(records, recent_tail_tokens=0)
    source_text = carried_compact_source_text(case.tool_source.source_records)
    middle = source_text.index("ORIGINAL_TOOL_15_MARKER")
    assert middle > 12_000 and len(source_text) - middle > 12_000
    ceiling = compact._compact_request_input_ceiling(case.agent, case.source.policy)
    assert estimate_tokens(source_text) >= ceiling
    sent, views = [], []

    def summary(request, **_kwargs):
        sent.append(request)
        calls = [] if response_kind == "empty" else [{
            "id": "must-not-execute", "name": "write_file",
            "input": {"path": "must-not-run.txt", "content": "不应执行"},
        }]
        return ModelResponse(text="" if not calls else "未执行的调用说明", backend="fake-summary", tool_use_blocks=calls)

    def project(view):
        views.append(view)
        tokens = estimate_tokens(view.summary) if view.is_candidate else ceiling + 1
        return ConversationCompactProjection(tokens, {"view": view, "surface": case.surface})

    monkeypatch.setattr(compact_request_budget, "generate_bounded_compact_response", summary)
    with pytest.raises(ConversationCompactError) as error:
        _compact(case, project)

    assert error.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    assert len(sent) == 1 and len(views) == 2
    supplied = sent[0].prompt + json.dumps(sent[0].messages, ensure_ascii=False)
    candidate = views[1]
    assert candidate.summary.endswith(source_text)
    for index in range(31):
        marker = f"ORIGINAL_TOOL_{index:02}_MARKER"
        assert marker in supplied and marker in candidate.summary
    assert "[conversation-compact-mechanical-fallback]" in candidate.summary
    assert candidate.retained_tool_records == (case.records[1],)
    assert not (tmp_path / "must-not-run.txt").exists()
    assert not case.checkpoint_path.exists()
    _assert_uncommitted(case, failures=1)


@pytest.mark.parametrize("second_result", ["oversized", "summary_failure"])
def test_mixed_fallback_commits_its_own_complete_candidate(mixed_case, monkeypatch, second_result):
    case = mixed_case
    summaries, projections = [], []
    ceiling = compact._compact_request_input_ceiling(case.agent, case.source.policy)
    assert case.source.policy.recovery_target_tokens < ceiling - 1

    def summary(_agent, _previous, evidence, rows, *, call):
        summaries.append((tuple(rows), evidence, call))
        if len(summaries) == 2 and second_result == "summary_failure":
            raise RuntimeError("第二次摘要失败")
        return f"候选阶段摘要-{len(summaries)}"

    def project(view):
        tokens = ceiling - 1 if len(projections) == 1 else ceiling + 1
        value = ConversationCompactProjection(tokens, {"view": view, "identity": object()})
        projections.append(value)
        return value

    monkeypatch.setattr(compact, "_summarize", summary)
    result = _compact(case, project, force=False)

    assert len(summaries) == 2 and len(case.commits) == 1
    selected = projections[1]
    view = selected.material["view"]
    assert result.request_projection is selected and result.request_projection.material is selected.material
    assert result.projected_tokens == ceiling - 1 and result.messages == view.messages
    assert view.messages and view.retained_tool_records is case.tool_source.retained_records
    assert view.summary == "候选阶段摘要-1"
    if second_result == "oversized":
        assert len(projections) == 3 and result.request_projection is not projections[-1]
        assert projections[-1].material["view"].messages == ()
        assert projections[-1].material["view"].operation_evidence != view.operation_evidence
    else:
        assert len(projections) == 2
    assert all(item[2].tool_source_records == case.tool_source.source_records for item in summaries)
    checkpoint, = committed_compact_checkpoint_chain(case.agent, result.thread)
    assert checkpoint["summary"] == view.summary
    assert checkpoint["operation_evidence"] == view.operation_evidence
    assert checkpoint["source_message_ids"] == [row.message_id for row in summaries[0][0]]
    assert checkpoint["retained_tail_message_ids"] == [row.message_id for row in view.messages]
    assert checkpoint["source_tool_refs"] == list(case.tool_source.source_tool_refs)
    assert checkpoint["retained_tool_refs"] == list(case.tool_source.retained_tool_refs)
    applied = resolve_compact_summary_view(case.agent, result.thread, THREAD_COMPACT_SCOPE)
    assert applied.summary == view.summary and applied.operation_evidence == view.operation_evidence
    assert set(applied.source_message_ids) == set(checkpoint["source_message_ids"])
    assert applied.source_tool_refs == case.tool_source.source_tool_refs
    assert result.thread.compact_source_tool_pairs == len(case.tool_source.source_tool_refs)


@pytest.mark.parametrize("stop_at", ["summary", "projection", "generic_failure_after_stop"])
def test_second_candidate_cancellation_never_commits_earlier_acceptable_candidate(mixed_case, monkeypatch, stop_at):
    case = mixed_case
    token = CancellationToken()
    summaries, projections, events = [], [], []
    ceiling = compact._compact_request_input_ceiling(case.agent, case.source.policy)

    def summary(*_args, **_kwargs):
        summaries.append(len(summaries) + 1)
        if len(summaries) == 2:
            if stop_at == "summary":
                raise ToolCancelled("第二次摘要已取消")
            if stop_at == "generic_failure_after_stop":
                token.cancel("第二候选期间停止")
                raise RuntimeError("停止之后出现普通错误")
        return f"候选摘要-{len(summaries)}"

    def project(view):
        if view.is_candidate and len(projections) == 2:
            raise ToolCancelled("第二次候选计量已取消")
        tokens = ceiling - 1 if view.is_candidate else ceiling + 1
        projection = ConversationCompactProjection(tokens, view)
        projections.append(projection)
        return projection

    monkeypatch.setattr(compact, "_summarize", summary)
    expected = InterruptedError if stop_at == "generic_failure_after_stop" else ToolCancelled
    with pytest.raises(expected):
        _compact(case, project, force=False, interrupt_check=lambda: token.cancelled, progress_callback=events.append)

    assert len(summaries) == len(projections) == 2
    assert case.source.policy.recovery_target_tokens < projections[1].projected_tokens < ceiling
    assert events[-1]["phase"] == "superseded"
    assert not case.checkpoint_path.exists()
    _assert_uncommitted(case, failures=0)


@pytest.mark.parametrize("typed_exception", [False, True])
def test_cancellation_from_committing_progress_leaves_checkpoint_unpublished(mixed_case, monkeypatch, typed_exception):
    case = mixed_case
    token, events = CancellationToken(), []
    monkeypatch.setattr(compact, "_summarize", lambda *_args, **_kwargs: "可发送的联合摘要")

    def progress(event):
        events.append(event)
        if event["stage"] == "committing" and event["percent"] == 92:
            assert case.checkpoint_path.exists() and case.commits == []
            if typed_exception:
                raise ToolCancelled("最终 CAS 前显式取消")
            token.cancel("最终 CAS 前取消")

    with pytest.raises(ToolCancelled if typed_exception else InterruptedError):
        _compact(
            case, lambda view: ConversationCompactProjection(100, view),
            interrupt_check=lambda: token.cancelled, progress_callback=progress,
        )

    assert any(event["stage"] == "committing" and event["percent"] == 92 for event in events)
    assert events[-1]["phase"] == "superseded"
    row, = [json.loads(line) for line in case.checkpoint_path.read_text(encoding="utf-8").splitlines()]
    assert row["source_message_ids"] and row["source_tool_refs"]
    _assert_uncommitted(case, failures=0)
