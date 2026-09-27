"""真实工具IR与archive的纯分区：pytest隔离环境中只构造原合同对象，不触发宿主IO。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.compact_tool_partition import (
    partition_recovery_tool_source,
    recovery_complete_ir_tool_refs,
    recovery_ir_tool_refs,
)
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.tooling.runtime_contracts import (
    ToolCall,
    ToolContentBlock,
    ToolResult,
    ToolResultRef,
    ToolSuccessFacts,
)


# LLM: 测试身份沿原ToolCall构造器生成，不从裸call_id推断turn；重复名字需要不同四元键。
# 函数用途: 创建一条可回放的原生工具调用。
def _call(call_id: str, *, turn: str, attempt: str = "attempt-1", run: str = "run-1") -> ToolCall:
    return ToolCall(call_id, "read_file", {"path": f"/{turn}.txt"}, "native", "sha256:fixture", run, turn, attempt)


# LLM: 归档保留原model_parameters和短预览，绝不借测试数据把预览冒充IR回执全文。
# 函数用途: 为原调用生成有完整四元身份的原archive记录。
def _record(call: ToolCall, *, preview: str = "短预览") -> dict[str, object]:
    return {
        "run_id": call.run_id, "attempt_id": call.attempt_id, "turn_id": call.turn_id,
        "call_id": call.call_id, "tool": call.tool_name, "ok": True,
        "model_parameters": {"path": call.arguments["path"], "nested": {"page": 1}},
        "output_preview": preview,
    }


def _ref(call: ToolCall) -> dict[str, str]:
    return {key: getattr(call, key) for key in ("run_id", "attempt_id", "turn_id", "call_id")}


def test_reused_call_id_in_different_turns_stays_exact_and_ir_full_text_wins():
    first = _call("same-id", turn="turn-1", attempt="attempt-1")
    second = _call("same-id", turn="turn-2", attempt="attempt-2")
    first_turn = AssistantTurn(text="第一轮完整说明", tool_calls=[first], content_blocks=[
        {"type": "text", "text": "第一轮完整说明"}, {"type": "tool_use", "id": first.call_id, "name": first.tool_name},
    ])
    second_turn = AssistantTurn(text="第二轮完整说明", tool_calls=[second])
    history = [first_turn, ToolResult.succeeded(first, "第一轮未截断的原文"),
               second_turn, ToolResult.succeeded(second, "第二轮未截断的原文")]
    records = [_record(first), _record(second)]

    source = partition_recovery_tool_source(records, history)

    assert source.source_records == tuple(records)
    assert source.source_tool_refs == (_ref(first), _ref(second))
    assert source.retained_tool_refs == ()
    assert source.retained_ir_history == ()
    assert source.source_ir_history == tuple(history)
    assert source.source_ir_history[1].output == "第一轮未截断的原文"
    assert source.source_ir_history[3].output == "第二轮未截断的原文"
    assert source.source_ir_history[0].text == "第一轮完整说明"
    records[0]["model_parameters"]["nested"]["page"] = 99
    assert source.source_records[0]["model_parameters"]["nested"]["page"] == 1


def test_duplicate_exact_ir_identity_and_archive_records_are_retained():
    duplicate = _call("same-id", turn="turn-1")
    independent = _call("other-id", turn="turn-2")
    duplicate_turn = AssistantTurn(tool_calls=[duplicate])
    history = [duplicate_turn, ToolResult.succeeded(duplicate, "第一次"),
               AssistantTurn(tool_calls=[deepcopy(duplicate)]), ToolResult.succeeded(duplicate, "第二次"),
               AssistantTurn(tool_calls=[independent]), ToolResult.succeeded(independent, "独立结果")]
    records = [_record(duplicate), deepcopy(_record(duplicate)), _record(independent)]

    source = partition_recovery_tool_source(records, history)

    assert source.source_records == (records[2],)
    assert source.retained_records == tuple(records[:2])
    assert source.source_tool_refs == (_ref(independent),)
    assert source.retained_tool_refs == (_ref(duplicate),)
    assert source.source_ir_history == tuple(history[4:])
    assert source.retained_ir_history == tuple(history[:4])


def test_complete_turn_can_source_when_same_bare_id_in_other_turn_is_incomplete():
    complete = _call("reused", turn="complete-turn")
    incomplete = _call("reused", turn="incomplete-turn")
    history = [AssistantTurn(tool_calls=[complete]), ToolResult.succeeded(complete, "完整原文"),
               AssistantTurn(tool_calls=[incomplete])]
    records = [_record(complete), _record(incomplete)]

    source = partition_recovery_tool_source(records, history)

    assert source.source_records == (records[0],)
    assert source.retained_records == (records[1],)
    assert source.source_tool_refs == (_ref(complete),)
    assert source.retained_tool_refs == (_ref(incomplete),)
    assert source.source_ir_history == tuple(history[:2])
    assert source.retained_ir_history == (history[2],)


def test_partial_parallel_group_orphan_and_user_facts_keep_chronology():
    first = _call("parallel-1", turn="turn-1")
    second = _call("parallel-2", turn="turn-1")
    complete = _call("complete", turn="turn-2")
    orphan = ToolResult.succeeded(_call("orphan", turn="before"), "无对应assistant")
    user = UserTurn("用户在工具期间补充的原文")
    facts = RuntimeFactsTurn("宿主事实", source="current")
    history = [orphan, AssistantTurn(text="并行工具", tool_calls=[first, second]),
               ToolResult.succeeded(first, "只有第一个结果"), user, facts,
               AssistantTurn(tool_calls=[complete]), ToolResult.succeeded(complete, "完整结果")]
    records = [_record(first), _record(second), _record(complete)]

    source = partition_recovery_tool_source(records, history)

    assert source.source_tool_refs == (_ref(complete),)
    assert source.retained_tool_refs == (_ref(first), _ref(second))
    assert source.source_ir_history == tuple(history[5:])
    assert source.retained_ir_history == tuple(history[:5])
    assert [type(item) for item in source.retained_ir_history] == [type(item) for item in history[:5]]
    assert source.retained_ir_history[3] == user and source.retained_ir_history[4] == facts


def test_completed_tool_pair_can_move_while_later_runtime_facts_stay_in_place():
    first = _call("first", turn="turn-1")
    second = _call("second", turn="turn-2")
    facts = RuntimeFactsTurn("下一模型请求的事实", source="prompt")
    user = UserTurn("工具已经结束后的插话")
    history = [AssistantTurn(tool_calls=[first]), ToolResult.succeeded(first, "完整结果"), facts, user,
               AssistantTurn(tool_calls=[second]), ToolResult.succeeded(second, "下一轮结果")]

    source = partition_recovery_tool_source([_record(first), _record(second)], history)

    assert source.source_tool_refs == (_ref(first), _ref(second))
    assert source.source_ir_history == (history[0], history[1], history[4], history[5])
    assert source.retained_ir_history == (facts, user)


def test_unowned_result_blocks_related_archive_without_guessing_turn():
    orphan = _call("orphan", turn="orphan-turn")
    independent = _call("independent", turn="valid-turn")
    history = [ToolResult.succeeded(orphan, "缺AssistantTurn"),
               AssistantTurn(tool_calls=[independent]), ToolResult.succeeded(independent, "完整结果")]

    source = partition_recovery_tool_source([_record(orphan), _record(independent)], history)

    assert source.source_tool_refs == (_ref(independent),)
    assert source.retained_tool_refs == (_ref(orphan),)
    assert source.retained_ir_history == (history[0],)


def test_covered_ref_is_retained_without_blocking_reused_bare_id():
    covered = _call("same-id", turn="old-turn")
    new = _call("same-id", turn="new-turn")
    history = [AssistantTurn(tool_calls=[covered]), ToolResult.succeeded(covered, "已覆盖"),
               AssistantTurn(tool_calls=[new]), ToolResult.succeeded(new, "未覆盖")]

    source = partition_recovery_tool_source([_record(covered), _record(new)], history,
                                            covered_tool_refs=(_ref(covered),))

    assert source.source_tool_refs == (_ref(new),)
    assert source.retained_tool_refs == (_ref(covered),)
    assert source.source_ir_history == tuple(history[2:])
    assert source.retained_ir_history == tuple(history[:2])


@pytest.mark.parametrize("unsafe_result", [
    lambda call: ToolResult.succeeded(call, facts=ToolSuccessFacts(content_blocks=(
        ToolContentBlock("text", text="媒体字节不能摘要", mime_type="image/png"),
    ))),
    lambda call: ToolResult.succeeded(call, facts=ToolSuccessFacts(content_blocks=(
        ToolContentBlock("ref", ref="artifact://unread-full-body"),
    ))),
])
def test_media_or_unread_ref_retains_group_and_associated_archive(unsafe_result):
    unsafe = _call("unsafe", turn="turn-1")
    safe = _call("safe", turn="turn-2")
    history = [AssistantTurn(tool_calls=[unsafe]), unsafe_result(unsafe),
               AssistantTurn(tool_calls=[safe]), ToolResult.succeeded(safe, "可完整摘要")]

    source = partition_recovery_tool_source([_record(unsafe), _record(safe)], history)

    assert source.source_tool_refs == (_ref(safe),)
    assert source.retained_tool_refs == (_ref(unsafe),)
    assert source.retained_ir_history == tuple(history[:2])


def test_unknown_assistant_block_and_interleaved_input_retain_entire_group():
    unknown = _call("unknown", turn="turn-1")
    interleaved = _call("interleaved", turn="turn-2")
    safe = _call("safe", turn="turn-3")
    history = [AssistantTurn(tool_calls=[unknown], content_blocks=[{"type": "image", "data": "opaque"}]),
               ToolResult.succeeded(unknown, "结果"), AssistantTurn(tool_calls=[interleaved]),
               UserTurn("不能移动到结果之后"), ToolResult.succeeded(interleaved, "结果"),
               AssistantTurn(tool_calls=[safe]), ToolResult.succeeded(safe, "结果")]

    source = partition_recovery_tool_source([_record(unknown), _record(interleaved), _record(safe)], history)

    assert source.source_tool_refs == (_ref(safe),)
    assert source.retained_tool_refs == (_ref(unknown), _ref(interleaved))
    assert source.retained_ir_history == tuple(history[:5])


def test_unknown_call_shape_never_mints_ir_ref_or_sources_related_archive():
    unknown = _call("unknown", turn="turn-1")
    safe = _call("safe", turn="turn-2")
    history = [AssistantTurn(tool_calls=[{"call_id": unknown.call_id, "tool_name": unknown.tool_name,
                                          **_ref(unknown)}]),
               ToolResult.succeeded(unknown, "原文"),
               AssistantTurn(tool_calls=[safe]), ToolResult.succeeded(safe, "完整结果")]

    source = partition_recovery_tool_source([_record(unknown), _record(safe)], history)

    assert source.source_tool_refs == (_ref(safe),)
    assert source.retained_tool_refs == (_ref(unknown),)
    assert recovery_ir_tool_refs(source.retained_ir_history) == ()


def test_partition_is_deep_copy_and_ir_only_source_is_valid():
    call = _call("ir-only", turn="turn-1")
    turn = AssistantTurn(text="保留完整助手正文", tool_calls=[call], content_blocks=[{"type": "text", "text": "原正文"}])
    history = [turn, ToolResult.succeeded(call, "保留完整工具结果")]
    source = partition_recovery_tool_source([], history)

    assert source.source_records == () and source.source_tool_refs == (_ref(call),)
    assert source.retained_ir_history == ()
    turn.content_blocks[0]["text"] = "后改"
    turn.tool_calls[0].arguments["path"] = "后改"
    assert source.source_ir_history[0].content_blocks[0]["text"] == "原正文"
    assert source.source_ir_history[0].tool_calls[0].arguments["path"] == "/turn-1.txt"
    assert source.source_ir_history[1].output == "保留完整工具结果"


def test_ir_ref_helper_rejects_incomplete_source_but_retained_collects_known_calls():
    call = _call("call", turn="turn-1")
    history = [AssistantTurn(tool_calls=[call]), UserTurn("插话")]

    assert recovery_ir_tool_refs(history) == (_ref(call),)
    with pytest.raises(ConversationCompactError, match="完整配对") as error:
        recovery_ir_tool_refs(history, require_complete=True)
    assert error.value.code == "COMPACT_TOOL_COVERAGE_UNKNOWN"
    assert partition_recovery_tool_source([], history) is None


def test_complete_ir_refs_only_select_confirmed_pairs_from_mixed_retained_history():
    safe = _call("safe", turn="turn-1")
    media = _call("media", turn="turn-2")
    partial = _call("partial", turn="turn-3")
    history = [AssistantTurn(tool_calls=[safe]), ToolResult.succeeded(safe, "完整文本"),
               RuntimeFactsTurn("下一轮事实", source="prompt"),
               AssistantTurn(tool_calls=[media]), ToolResult.succeeded(media, facts=ToolSuccessFacts(
                   content_blocks=(ToolContentBlock("ref", ref="artifact://media"),),
               )),
               AssistantTurn(tool_calls=[partial]), UserTurn("未完成工具前的插话")]

    assert recovery_complete_ir_tool_refs(history) == (_ref(safe), _ref(media))
    assert recovery_ir_tool_refs(history) == (_ref(safe), _ref(media), _ref(partial))
    with pytest.raises(ConversationCompactError):
        recovery_ir_tool_refs(history, require_complete=True)


# LLM: 仿照 archive_tool_output_projection 的真实回执形状：正文（外置时是预览）+ 引用块 + typed ToolResultRef；
#   引用上的 sha256/size_bytes 与生产一样取本次回执的值，判据只比对引用值本身。
# 函数用途: 生成一条带引用的原生工具回执。
def _ref_result(call: ToolCall, ref_value: str, *, text: str = "外置预览", mime_type: str = "",
                extra_blocks: tuple[ToolContentBlock, ...] = ()) -> ToolResult:
    ref = ToolResultRef(kind="tool_output", ref=ref_value, sha256="sha256:this-receipt", size_bytes=716,
                        summary="complete raw tool output", mime_type=mime_type)
    blocks = (ToolContentBlock("text", text=text), ToolContentBlock("ref", ref=ref_value), *extra_blocks)
    return ToolResult.succeeded(call, facts=ToolSuccessFacts(content_blocks=blocks, refs=(ref,)))


def test_text_result_whose_ref_is_its_archived_output_can_be_sourced():
    # 2026-09-27 G02 最小复现：修复前任何带引用回执都判不完整，整组留在保留区、归档记录被连带排除，
    # 分区返回 None，强制恢复在 compact_request_recovery 报 COMPACT_TOOL_COVERAGE_UNKNOWN。
    call = _call("search", turn="turn-1")
    path = "/owner/tool_outputs/search.json"
    history = [AssistantTurn(tool_calls=[call]), _ref_result(call, path)]
    record = {**_record(call), "output_externalized": True, "output_path": path, "artifact_ref": path}

    source = partition_recovery_tool_source([record], history)

    assert source is not None
    assert source.source_tool_refs == (_ref(call),) and source.source_records == (record,)
    assert source.source_ir_history == tuple(history)
    assert source.retained_ir_history == () and source.retained_tool_refs == ()


def test_read_artifact_source_ref_is_matched_by_its_recorded_value():
    # read_artifact 的引用指向被读调用的逻辑引用（scoped_call_id），不是本次回执的全文；归档把它记在 source_artifact_ref。
    reader = _call("reader", turn="turn-2")
    scoped = "run-1:search"
    history = [AssistantTurn(tool_calls=[reader]), _ref_result(reader, scoped, text='{"content": "完整读取回执"}')]
    record = {**_record(reader), "reads_artifact_body": True, "source_artifact_ref": scoped,
              "source_call_id": "search", "output_path": ""}

    source = partition_recovery_tool_source([record], history)

    assert source.source_tool_refs == (_ref(reader),)
    assert source.source_ir_history == tuple(history)


_UNSAFE_PATH = "/owner/tool_outputs/unsafe.json"


def _recorded(call: ToolCall, **fields: object) -> list[dict[str, object]]:
    return [{**_record(call), "output_path": _UNSAFE_PATH, **fields}]


# 每种情形：(该调用的归档记录, 该调用的回执)。工具自报引用时顶层 tool_result_refs 只是回执引用的副本，归档并不为它保存内容。
_RETAINED_CASES = {
    "unrecorded": (lambda call: _recorded(call, output_path="/owner/tool_outputs/other.json"),
                   lambda call: _ref_result(call, _UNSAFE_PATH)),
    "handler_declared": (lambda call: [{**_record(call), "tool_result_refs": [{"kind": "artifact", "ref": _UNSAFE_PATH}],
                                        "tool_result_envelope": {"tool_result_refs": [{"ref": _UNSAFE_PATH}]}}],
                         lambda call: _ref_result(call, _UNSAFE_PATH)),
    "media": (_recorded, lambda call: _ref_result(call, _UNSAFE_PATH, mime_type="image/png")),
    "json_block": (_recorded, lambda call: _ref_result(
        call, _UNSAFE_PATH, extra_blocks=(ToolContentBlock("json", data={"rows": 3}),))),
    "text_with_data": (_recorded, lambda call: _ref_result(
        call, _UNSAFE_PATH, extra_blocks=(ToolContentBlock("text", text="附带数据", data={"rows": 3}),))),
    "no_archive": (lambda call: [], lambda call: _ref_result(call, _UNSAFE_PATH)),
}


@pytest.mark.parametrize("case", list(_RETAINED_CASES))
def test_ref_results_outside_the_archive_own_fields_stay_retained(case):
    records_for, result_for = _RETAINED_CASES[case]
    call = _call("unsafe", turn="turn-1")
    safe = _call("safe", turn="turn-2")
    records = records_for(call)
    history = [AssistantTurn(tool_calls=[call]), result_for(call),
               AssistantTurn(tool_calls=[safe]), ToolResult.succeeded(safe, "可完整摘要")]

    source = partition_recovery_tool_source([*records, _record(safe)], history)

    assert source.source_tool_refs == (_ref(safe),)
    assert source.retained_tool_refs == (_ref(call),)
    assert source.retained_records == tuple(records)
    assert source.retained_ir_history == tuple(history[:2])


def test_carried_source_rechecks_ref_results_against_its_own_archive_records():
    call = _call("search", turn="turn-1")
    path = "/owner/tool_outputs/search.json"
    record = {**_record(call), "output_path": path}
    source = partition_recovery_tool_source([record], [AssistantTurn(tool_calls=[call]), _ref_result(call, path)])

    with pytest.raises(ConversationCompactError, match="完整配对") as error:
        replace(source, source_records=({**record, "output_path": "/owner/tool_outputs/other.json"},))
    assert error.value.code == "COMPACT_TOOL_COVERAGE_UNKNOWN"
