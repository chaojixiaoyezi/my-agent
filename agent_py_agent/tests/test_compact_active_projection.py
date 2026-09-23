"""活动工具完整恢复只替换结构化IR来源，并保留冻结输入及控制事实。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.compact_active_projection import replace_recovery_active_tools
from agent_py_agent.agent.agent_core.runtime.conversation_state import (
    conversation_runtime_state_section,
)
from agent_py_agent.agent.agent_core.tool_ir_history import project_native_provider_messages
from agent_py_agent.agent.agent_core.tool_request_projection import ToolLoopRequestInput
from agent_py_agent.agent.backends.tool_ir import (
    AssistantTurn,
    CompactionSummary,
    RuntimeFactsTurn,
    ToolCall,
    ToolResult,
    UserTurn,
)
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_scope import CompactScope
from agent_py_agent.agent.conversation.compact_summary_view import (
    AppliedCompactContext,
    CompactSummaryView,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolContentBlock


# LLM: 测试的完整archive多于模型保留集，候选只能减少展示，不可缩短预算和审批事实。
# 函数用途: 构造可投影的原活动轮参数和冻结原生IR，确保媒体前缀与插话可逐项比对。
def _fixture(*, seed=None, source="carried_tool_handoff", guidance_forwarded=False):
    context = AppliedCompactContext(
        "thread-1", CompactScope(kind="turn", turn_id="turn-1"),
        CompactSummaryView(summary="新范围摘要", generation=2),
    )
    media = {"role": "user", "content": [
        {"type": "text", "text": "原图片说明"},
        {"type": "image", "source": {"type": "base64", "data": "image-bytes"}},
    ]}
    provider_history = ([{
        "role": "user", "content": "# Earlier Conversation Summary (generation 2)\n新范围摘要",
    }] if seed is not None else []) + [media]
    guidance = "本轮未转发的运行时指引"
    history = [
        UserTurn("原任务 [image: asset-1]"),
        CompactionSummary("# Earlier Conversation Summary (generation 1)\n旧摘要", source="applied_compact"),
        CompactionSummary("[active-turn-tool-handoff]\n旧交接", source=source),
        UserTurn("用户在模型运行时补充的新要求"),
        RuntimeFactsTurn("当前控制事实", source="runtime-state"),
        AssistantTurn(text="已读事实", content_blocks=[{"type": "text", "text": "保留块"}]),
        CompactionSummary("[active-turn-tool-handoff] 只是正文", source=""),
    ]
    if guidance_forwarded:
        history.append(RuntimeFactsTurn(guidance, source="runtime.guidance"))
    full_archive = [
        {"call_id": "call-a", "scoped_call_id": "run-1:call-a", "tool": "read_file", "ok": True,
         "model_parameters": {"path": "a.txt"}, "model_summary": "保留的结果"},
        {"call_id": "call-b", "scoped_call_id": "run-1:call-b", "tool": "write_file", "ok": True,
         "model_parameters": {"path": "b.txt"}, "model_summary": "已被摘要覆盖的结果"},
    ]
    params = ToolLoopExecuteParams(
        user_prompt="继续", memories=[], runtime_injections=["原注入"], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[
            "[tool-record carried tool=read_file]\n旧工具正文", guidance,
        ],
        effective_on_chunk=None, allowed_tools=None, write_boundary={"root": "work"},
        task_attributes={"conversation_thread_id": "thread-1"}, request_id="request-1",
        run_id="run-1", task_id="task-1", one_shot_tool_calls={"already-used"},
        executed_tools=["read_file", "write_file"], archive_tool_calls=full_archive,
        tool_rounds=7, live_archive_state={
            "approval": {"token": "approved"},
            "_forwarded_runtime_guidance": {guidance} if guidance_forwarded else set(),
        },
        runtime_approved_actions=[{"id": "approval-1"}],
        runtime_rejected_actions=[{"id": "rejection-1"}],
        tool_ir_history=history, provider_history_messages=provider_history,
        conversation_history_seed=seed, compact_context=context,
    )
    frozen = ToolLoopRequestInput(
        tool_ir_history=tuple(history), provider_history_messages=tuple(provider_history),
        tool_context=tuple(params.tool_context), forwarded_guidance=(
            frozenset({guidance}) if guidance_forwarded else frozenset()
        ), conversation_state=conversation_runtime_state_section(params),
    )
    return context, params, frozen, full_archive[:1]


def test_replaces_only_structured_handoff_and_applied_summary_without_mutating_inputs():
    context, params, frozen, retained = _fixture()
    before_ir, before_archive = deepcopy(params.tool_ir_history), deepcopy(params.archive_tool_calls)
    before_frozen = deepcopy(frozen)

    candidate, prepared = replace_recovery_active_tools(
        params, frozen, compact_context=context, retained_records=tuple(retained), max_chars=2_000,
    )

    assert params.tool_ir_history == before_ir and params.archive_tool_calls == before_archive
    assert frozen == before_frozen
    assert [type(item) for item in prepared.tool_ir_history] == [
        UserTurn, CompactionSummary, CompactionSummary, UserTurn,
        RuntimeFactsTurn, AssistantTurn, CompactionSummary,
    ]
    assert prepared.tool_ir_history[0] == frozen.tool_ir_history[0]
    assert prepared.tool_ir_history[3:] == frozen.tool_ir_history[3:]
    assert prepared.tool_ir_history[1] == CompactionSummary(
        "# Earlier Conversation Summary (generation 2)\n新范围摘要", source="applied_compact",
    )
    assert "call-a" in prepared.tool_ir_history[2].text
    assert "call-b" not in prepared.tool_ir_history[2].text
    assert prepared.tool_ir_history[2].source == "carried_tool_handoff"
    assert prepared.provider_history_messages == frozen.provider_history_messages
    assert candidate.provider_history_messages == list(prepared.provider_history_messages)
    assert candidate.tool_context == list(prepared.tool_context)
    assert candidate.tool_context[-1] == "本轮未转发的运行时指引"
    assert candidate.tool_ir_history == list(prepared.tool_ir_history)
    assert '"compact_generation":2' in prepared.conversation_state
    assert '"authority":"conversation_thread.compact_generation"' in prepared.conversation_state
    assert candidate.archive_tool_calls is params.archive_tool_calls
    assert candidate.tool_rounds == 7 and candidate.one_shot_tool_calls == {"already-used"}
    assert candidate.runtime_approved_actions is params.runtime_approved_actions
    assert candidate.runtime_rejected_actions is params.runtime_rejected_actions
    assert candidate.live_archive_state is not params.live_archive_state
    assert params.live_archive_state == {
        "approval": {"token": "approved"}, "_forwarded_runtime_guidance": set(),
    }


def test_seed_prefix_owns_summary_and_empty_retained_set_removes_only_handoff():
    context, params, frozen, _ = _fixture(seed=SimpleNamespace(compact_generation=2))
    candidate, prepared = replace_recovery_active_tools(
        params, frozen, compact_context=context, retained_records=[], max_chars=2_000,
    )

    assert not any(isinstance(item, CompactionSummary) and item.source in {
        "applied_compact", "carried_tool_handoff",
    } for item in prepared.tool_ir_history)
    assert prepared.tool_ir_history == (
        frozen.tool_ir_history[0], *frozen.tool_ir_history[3:],
    )
    assert prepared.provider_history_messages == frozen.provider_history_messages
    assert prepared.provider_history_messages[0]["content"].count("新范围摘要") == 1
    assert prepared.tool_context == ("本轮未转发的运行时指引",)
    assert candidate.tool_context == ["本轮未转发的运行时指引"]
    assert candidate.archive_tool_calls == params.archive_tool_calls
    assert candidate.tool_rounds == params.tool_rounds


def test_seedless_candidate_inserts_summary_once_when_original_ir_lacks_applied_item():
    context, params, frozen, retained = _fixture()
    without_applied = tuple(item for item in frozen.tool_ir_history if not (
        isinstance(item, CompactionSummary) and item.source == "applied_compact"
    ))
    params = replace(params, tool_ir_history=list(without_applied))
    frozen = replace(frozen, tool_ir_history=without_applied)

    _, prepared = replace_recovery_active_tools(
        params, frozen, compact_context=context, retained_records=retained, max_chars=2_000,
    )

    assert prepared.tool_ir_history[0] == frozen.tool_ir_history[0]
    assert prepared.tool_ir_history[1].source == "applied_compact"
    assert sum(isinstance(item, CompactionSummary) and item.source == "applied_compact"
               for item in prepared.tool_ir_history) == 1


def test_guidance_already_in_ir_is_not_forwarded_again():
    context, params, frozen, retained = _fixture(guidance_forwarded=True)
    candidate, prepared = replace_recovery_active_tools(
        params, frozen, compact_context=context, retained_records=tuple(retained), max_chars=2_000,
    )

    assert all("本轮未转发的运行时指引" not in entry for entry in candidate.tool_context)
    assert prepared.tool_ir_history[-1] == RuntimeFactsTurn(
        "本轮未转发的运行时指引", source="runtime.guidance",
    )
    messages = project_native_provider_messages(
        prepared.tool_ir_history, prior_messages=prepared.provider_history_messages,
        tool_context=prepared.tool_context, forwarded_guidance=prepared.forwarded_guidance,
    )
    assert str(messages).count("本轮未转发的运行时指引") == 1


@pytest.mark.parametrize("complete", [False, True])
def test_explicit_retained_ir_deduplicates_only_complete_exact_replay(complete):
    context, params, frozen, records = _fixture()
    records[0].update(run_id="run-1", attempt_id="attempt-1", turn_id="turn-1")
    records[0]["model_summary"] = "归档回执标记"
    call = ToolCall("call-a", "read_file", {}, "native", "sha256:test", "run-1", "turn-1", "attempt-1")
    tail = [AssistantTurn(tool_calls=[call])]
    if complete:
        tail.append(ToolResult("call-a", "read_file", "succeeded", (
            ToolContentBlock("text", text="原生回执全文标记"),
        )))
    retained_ir = (UserTurn("原任务"), *tail, RuntimeFactsTurn("后续指引", source="runtime.guidance"))
    before = deepcopy(retained_ir)
    candidate, prepared = replace_recovery_active_tools(
        params, frozen, compact_context=context, retained_records=records,
        retained_ir_history=retained_ir, max_chars=2_000,
    )
    messages = project_native_provider_messages(
        prepared.tool_ir_history, prior_messages=prepared.provider_history_messages,
        tool_context=prepared.tool_context, forwarded_guidance=prepared.forwarded_guidance,
    )
    assert ("归档回执标记" in str(messages)) is not complete
    assert ("原生回执全文标记" in str(messages)) is complete
    assert prepared.tool_ir_history[-len(tail) - 1:] == (*tail, retained_ir[-1])
    assert retained_ir == before
    assert candidate.archive_tool_calls is params.archive_tool_calls


def test_explicit_retained_archive_is_inserted_without_an_original_handoff():
    context, params, frozen, records = _fixture()
    retained_ir = (UserTurn("原任务"), UserTurn("后续插话"), RuntimeFactsTurn("后续事实", source="runtime.state"))
    _, prepared = replace_recovery_active_tools(
        params, frozen, compact_context=context, retained_records=records,
        retained_ir_history=retained_ir, max_chars=2_000,
    )
    assert prepared.tool_ir_history[0] == retained_ir[0]
    assert prepared.tool_ir_history[1].source == "applied_compact"
    assert prepared.tool_ir_history[2].source == "carried_tool_handoff"
    assert "保留的结果" in prepared.tool_ir_history[2].text
    assert prepared.tool_ir_history[3:] == retained_ir[1:]


@pytest.mark.parametrize("mutation", [
    "legacy_marker", "no_handoff", "duplicate_handoff", "duplicate_applied",
    "real_result", "real_call", "empty_summary",
])
def test_unknown_ir_does_not_project_a_fabricated_handoff(mutation):
    context, params, frozen, retained = _fixture()
    history = list(frozen.tool_ir_history)
    if mutation == "legacy_marker":
        history[2] = replace(history[2], source="")
    elif mutation == "no_handoff":
        del history[2]
    elif mutation == "duplicate_handoff":
        history.append(history[2])
    elif mutation == "duplicate_applied":
        history.append(history[1])
    elif mutation == "real_result":
        history.append(ToolResult("call-x", "read_file", "succeeded"))
    elif mutation == "real_call":
        history.append(AssistantTurn(tool_calls=[ToolCall(
            "call-x", "read_file", {}, "native", "sha256:test", "run-1", "turn-1", "attempt-1",
        )]))
    else:
        context = replace(context, view=replace(context.view, summary=""))
        params = replace(params, compact_context=context)
    params = replace(params, tool_ir_history=history)
    frozen = replace(frozen, tool_ir_history=tuple(history))
    before_ir, before_archive = deepcopy(params.tool_ir_history), deepcopy(params.archive_tool_calls)

    with pytest.raises(ConversationCompactError) as error:
        replace_recovery_active_tools(
            params, frozen, compact_context=context, retained_records=retained, max_chars=2_000,
        )
    assert error.value.code == "COMPACT_REQUEST_PROJECTION_UNKNOWN"
    assert params.tool_ir_history == before_ir and params.archive_tool_calls == before_archive
