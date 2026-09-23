"""后台作用域沿真实 Store、checkpoint/CAS 与原生模型投影恢复，只有摘要模型使用替身。"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import (
    RuntimeLoopParams,
    RuntimeToolLoopSeed,
)
from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_loop_execute_params
from agent_py_agent.agent.agent_core.tool_model_generation import _native_provider_messages
from agent_py_agent.agent.backends.tool_ir import CompactionSummary
from agent_py_agent.agent.conversation import compact as compact_module
from agent_py_agent.agent.conversation.active_turn_compact import (
    ActiveTurnArchiveCompactRequest,
    compact_carried_active_turn_archive,
    model_visible_active_turn_tool_calls,
)
from agent_py_agent.agent.conversation.background_compact_context import (
    apply_background_compact_context,
)
from agent_py_agent.agent.conversation.background_context import (
    prepare_background_context,
    render_background_context,
)
from agent_py_agent.agent.conversation.background_history_seed import (
    prepare_background_history_or_raise,
    refresh_background_history,
)
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_checkpoint import (
    committed_compact_checkpoint_chain,
)
from agent_py_agent.agent.conversation.compact_tool_identity import compact_tool_ref
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _background_task_attributes,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import compact_semantic_summary
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    make_test_protocol_snapshot,
)


# LLM: 只替身摘要生成；初始化和后续提交均使用临时目录内的真实 Agent、Store 与原 Compact 链。
# 函数用途: 构造含创建前材料、独立任务材料和兄弟材料的会话，记录每次真实摘要的来源。
@pytest.fixture
def background_case(tmp_path, monkeypatch):
    config = AgentConfig(
        model_backend="echo", my_agent_home=str(tmp_path / "home"), prompt_files=[],
        max_tokens=64, model_context_window_tokens=1_000_000,
        memory_compact_auto_trigger_percent=50,
    )
    config.config_sources = {"model_context_window_tokens": {"source": "test"}}
    agent = SimpleAgent(config, tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "scope-owner", "channel": "tui",
        "channel_conversation_id": "background-scope", "channel_user_id": "scope-owner",
    })
    case = SimpleNamespace(agent=agent, store=store, thread=thread, summaries=[])
    before = _append(case, "PRE_CREATION_EVIDENCE", now=50.0)
    store.tasks.bind({
        "thread_id": thread.thread_id, "task_id": "task-A", "goal": "持续核对资料",
        "status": "active", "work_kind": "audit", "work_name": "saved-A",
        "cancellation_scope": "detached", "now": 100.0,
    })
    own = _append(case, "TASK_A_LATER_EVIDENCE", now=150.0, task_id="task-A")
    sibling = _append(case, "UNRELATED_TASK_B_SECRET", now=200.0, task_id="task-B")
    case.thread = store.threads.require(thread.thread_id)
    case.rows = (before, own, sibling)

    def summarize(_agent, previous, evidence, rows, **_kwargs):
        case.summaries.append((previous, tuple(row.message_id for row in rows)))
        return "；".join([previous, *(row.content for row in rows)]).strip("；")

    monkeypatch.setattr(compact_module, "_summarize", summarize)
    return case


# LLM: 来源身份随真实消息 append 一起写入，测试不靠正文选择任务范围。
# 函数用途: 向临时 canonical 历史追加有明确时间和任务归属的一行。
def _append(case, content, *, now, task_id=""):
    return case.store.messages.append({
        "thread_id": case.thread.thread_id, "role": "user", "content": content,
        "channel": "tui", "now": now,
        "metadata": {"conversation_request_id": content,
                     **({"conversation_task_id": task_id} if task_id else {})},
    })


# LLM: 请求回合是显式宿主身份，不复用 task ID；调用方可验证另一活动轮不继承本轮摘要。
# 函数用途: 创建普通 detached 续做或 narrow 审计请求。
def _request(case, *, reason="scheduled_progress_report", turn_id="background-turn-A"):
    return BackgroundRunRequest(
        thread_id=case.thread.thread_id, task_id="task-A", reason=reason,
        conversation_turn_id=turn_id,
    )


# LLM: 仍走真实 transcript 候选、原 writer 与 generation CAS，不手填线程 summary 或游标。
# 函数用途: 先建立覆盖所有消息的全线程摘要，复现任务创建后的全局压缩交错。
def _global_compact(case):
    result = prepare_conversation_context(
        case.agent, case.store, case.store.threads.require(case.thread.thread_id),
        options=ConversationCompactOptions(current_prompt="继续会话", force=True),
    )
    assert result.compacted
    assert result.thread.compacted_through_message_id == case.rows[-1].message_id
    assert result.thread.compacted_through_byte_offset > 0
    return result.thread


# LLM: 只比较全线程摘要展示字段，链 head/generation 与累计来源计数本就随局部 CAS 推进。
# 函数用途: 捕获局部摘要提交不得改写的全线程投影。
def _thread_projection(thread):
    return tuple(deepcopy(getattr(thread, name)) for name in (
        "summary", "compact_operation_evidence", "compacted_through_message_id",
        "compacted_through_byte_offset", "compact_updated_at",
    ))


# LLM: 归档四元身份来自原 ToolCall；测试未知旧记录时只移除字段，不能补当前 attempt。
# 函数用途: 创建一条可沿原 Compact 精确匹配的已执行工具记录。
def _tool_record(*, turn_id="model-A-1", evidence="TASK_A_TOOL_EVIDENCE"):
    call = canonical_history_call(
        "read_file", {"path": "evidence.txt"}, call_id="call-A-1",
        run_id="run-A", attempt_id="attempt-A", turn_id=turn_id,
    )
    return {
        **call.to_dict(), "tool": call.tool_name, "ok": True,
        "parameters": dict(call.arguments), "model_parameters": dict(call.arguments),
        "output_preview": evidence, "tool_round": 1,
    }


# LLM: 摘要仍满足生产六字段格式，身份和范围断言不读取这些文案。
# 函数用途: 提供带可追踪材料的无网络活动回合摘要。
def _live_summary():
    return (
        "[compact-live-handoff.v1]\ncurrent_progress: TASK_A_TOOL_EVIDENCE\n"
        "user_constraints: 保持原任务范围\ncompleted: 已读文件\nfailures: 无\n"
        "unresolved: 继续核对\nnext_step: 检查下一项"
    )


# LLM: 原 RuntimeLoopParams 和 ToolLoop 负责完整归档、模型过滤与原生摘要注入；不手写预期 provider 消息。
# 函数用途: 用后台准备结果生成真实 native 出站消息投影，不调用后端或执行工具。
def _native_request(case, history, request, records):
    attrs = _background_task_attributes(case.thread.thread_id, request, case.agent, thread=case.thread)
    params = RuntimeLoopParams(
        user_prompt="继续本轮核对", root_user_prompt="继续本轮核对", memories=[],
        runtime_injections=[], routed_context=None, resume_context_section="",
        request_id="request-A", run_id="run-A", task_id="task-A", attempt_id="attempt-A",
        task_attributes=attrs, conversation_history_seed=history.seed,
        compact_context=history.compact_context, carried_archive_tool_calls=records,
    )
    loop = _tool_loop_execute_params(case.agent, RuntimeToolLoopSeed(
        params=params, memories=[], tool_catalog_section="", tool_recommendations_section="",
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="run-A"),
    ))
    return loop, json.dumps(_native_provider_messages(case.agent, loop), ensure_ascii=False)


def test_detached_history_survives_later_global_compact_without_sibling_summary(background_case):
    case = background_case
    request = _request(case)
    before = prepare_background_history_or_raise(case.agent, case.store, case.thread, request)
    global_thread = _global_compact(case)
    after = prepare_background_history_or_raise(case.agent, case.store, global_thread, request)

    assert before.status == after.status == "ready"
    assert after.compact_context.scope.kind == "task"
    assert before.seed.messages == after.seed.messages == (
        ("user", "PRE_CREATION_EVIDENCE"), ("user", "TASK_A_LATER_EVIDENCE"),
    )
    assert after.seed.compact_summary == after.compact_context.view.summary == ""
    assert tuple(row.message_id for row in after.compact_source.messages) == tuple(
        row.message_id for row in case.rows[:2]
    )
    assert len(case.store.messages.recent(case.thread.thread_id, limit=0)) == 3
    _, payload = _native_request(case, after, request, [])
    assert "PRE_CREATION_EVIDENCE" in payload and "TASK_A_LATER_EVIDENCE" in payload
    assert "UNRELATED_TASK_B_SECRET" not in payload


def test_detached_transcript_source_commits_locally_and_preserves_global_projection(background_case):
    case = background_case
    request = _request(case)
    global_thread = _global_compact(case)
    original_projection = _thread_projection(global_thread)
    history = prepare_background_history_or_raise(case.agent, case.store, global_thread, request)
    result = prepare_conversation_context(
        case.agent, case.store, global_thread,
        options=ConversationCompactOptions(
            current_prompt="继续本任务", force=True, source=history.compact_source,
        ),
    )
    assert result.compacted and result.thread.compact_generation == global_thread.compact_generation + 1
    assert _thread_projection(result.thread) == original_projection
    checkpoint = committed_compact_checkpoint_chain(case.agent, result.thread)[-1]
    assert checkpoint["scope"] == history.compact_context.scope.to_dict()
    assert checkpoint["summary_base_checkpoint_id"] == ""
    assert checkpoint["source_message_ids"] == [row.message_id for row in case.rows[:2]]
    assert case.summaries[-1] == ("", tuple(row.message_id for row in case.rows[:2]))

    refreshed = refresh_background_history(case.agent, case.store, result.thread, history)
    assert refreshed.compact_context.scope == history.compact_context.scope
    assert refreshed.compact_context.view.checkpoint_id == result.thread.compact_checkpoint_id
    assert refreshed.seed.messages == ()
    assert refreshed.compact_source.messages == ()
    assert "UNRELATED_TASK_B_SECRET" not in refreshed.seed.compact_summary
    _, payload = _native_request(case, refreshed, request, [])
    assert "PRE_CREATION_EVIDENCE" in payload and "TASK_A_LATER_EVIDENCE" in payload
    assert "UNRELATED_TASK_B_SECRET" not in payload

    prepared = prepare_background_context(
        agent=case.agent, store=case.store, thread=result.thread,
        request=request, include_recent_messages=False,
    )
    rendered = render_background_context(apply_background_compact_context(
        prepared, refreshed.compact_context, result.thread.compact_generation,
    ))
    assert "TASK_A_LATER_EVIDENCE" in rendered
    assert "UNRELATED_TASK_B_SECRET" not in rendered


@pytest.mark.parametrize("reason,kind", [("scheduled_progress_report", "task"), ("audit_finding", "turn")])
def test_scoped_active_compact_hides_only_with_its_applied_summary(background_case, monkeypatch, reason, kind):
    case = background_case
    request = _request(case, reason=reason)
    global_thread = _global_compact(case)
    original_projection = _thread_projection(global_thread)
    before = prepare_background_history_or_raise(case.agent, case.store, global_thread, request)
    attrs = _background_task_attributes(case.thread.thread_id, request, case.agent, thread=global_thread)
    records = [_tool_record()]
    original_records = deepcopy(records)
    summary_calls = []

    def summarize(request):
        summary_calls.append(request)
        return _live_summary()

    monkeypatch.setattr(compact_semantic_summary, "summarize_live_tool_history", summarize)
    compacted = compact_carried_active_turn_archive(
        case.agent, case.store, global_thread, records,
        ActiveTurnArchiveCompactRequest(
            task_attributes=attrs, compact_context=before.compact_context,
            request_id="request-A", attempt_id="attempt-A", task_prompt="继续本轮核对",
        ),
    )
    assert compacted.compacted and len(summary_calls) == 1
    assert summary_calls[0].previous_summary == ""
    assert records == original_records
    assert _thread_projection(compacted.thread) == original_projection
    checkpoint = committed_compact_checkpoint_chain(case.agent, compacted.thread)[-1]
    assert checkpoint["scope"] == before.compact_context.scope.to_dict()
    assert checkpoint["source_tool_refs"] == [compact_tool_ref(records[0])]
    assert checkpoint["summary_base_checkpoint_id"] == ""

    after = refresh_background_history(case.agent, case.store, compacted.thread, before)
    assert after.compact_context.scope.kind == kind
    assert after.compact_context.view.checkpoint_id == compacted.thread.compact_checkpoint_id
    assert model_visible_active_turn_tool_calls(
        case.agent, attrs, records, compact_context=before.compact_context,
    ) == records
    unknown = dict(records[0], output_preview="UNKNOWN_TOOL_EVIDENCE")
    unknown.pop("turn_id")
    later = _tool_record(turn_id="model-A-2", evidence="LATER_TOOL_EVIDENCE")
    archive = [*records, unknown, later]
    assert model_visible_active_turn_tool_calls(
        case.agent, attrs, archive, compact_context=after.compact_context,
    ) == [unknown, later]
    loop, payload = _native_request(case, after, request, archive)
    assert loop.archive_tool_calls == archive
    assert payload.count("TASK_A_TOOL_EVIDENCE") == 1
    assert "UNKNOWN_TOOL_EVIDENCE" in payload and "LATER_TOOL_EVIDENCE" in payload
    assert "UNRELATED_TASK_B_SECRET" not in payload
    applied_summaries = [item for item in loop.tool_ir_history
                         if isinstance(item, CompactionSummary) and "TASK_A_TOOL_EVIDENCE" in item.text]
    if kind == "turn":
        assert before.seed is after.seed is None
        assert after.status == "disabled" and after.seed is None
        assert after.compact_source is not None and after.compact_source.messages == ()
        assert after.compact_source.compact_context == after.compact_context
        assert len(applied_summaries) == 1 and loop.provider_history_messages == []
        next_event = prepare_background_history_or_raise(
            case.agent, case.store, compacted.thread,
            _request(case, reason=reason, turn_id="background-turn-next"),
        )
        assert next_event.seed is None and next_event.compact_context.view.summary == ""
        assert model_visible_active_turn_tool_calls(
            case.agent, attrs, records, compact_context=next_event.compact_context,
        ) == records
    else:
        assert after.seed is not None and after.seed.compact_summary == _live_summary()
        assert applied_summaries == []


def test_background_overflow_refresh_retains_original_scope_bundle(background_case, monkeypatch):
    from agent_py_agent.agent.conversation import background_history_seed as history_module

    case = background_case
    request = _request(case)
    before = prepare_background_history_or_raise(case.agent, case.store, case.thread, request)
    assert before.compact_context.scope.kind == "task"
    assert before.context_bundle is not None
    _append(case, "LATE_SIBLING_MATERIAL", now=250.0, task_id="task-B")

    def reject_scope_reload(*args, **kwargs):
        raise AssertionError("同片恢复不得重新裁决任务范围")

    monkeypatch.setattr(history_module, "load_context_bundle", reject_scope_reload)
    latest = case.store.threads.require(case.thread.thread_id)
    after = refresh_background_history(case.agent, case.store, latest, before)
    assert after.context_bundle is before.context_bundle
    assert after.projection.scope == before.projection.scope
    assert after.compact_context.scope == before.compact_context.scope
    assert [row.message_id for row in after.compact_source.messages] == [
        row.message_id for row in before.compact_source.messages
    ]
