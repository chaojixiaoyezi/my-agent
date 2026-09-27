# LLM: 原生IR与归档只按完整四元调用及同一AssistantTurn内的结果配对分区；未知正文、媒体、插话和重复身份保留原位。
#   回执带引用时，只有每个引用都等于同一四元身份原归档记录自己写下的输出位置（外置全文路径、分页读取的来源引用），
#   整组才可移入摘要来源。摘要材料始终是模型当时看到的原回执（预览/正文+指针），不按引用去取全文；
#   指针指向的内容由归档按四元身份保存，压缩后仍可读回。工具自报的其它引用、媒体引用、json/数据块仍保留原位。
# 模块用途: 纯计算Compact可替代的完整工具往返，交回原CarriedToolCompactSource，不读取存储或调用摘要器。
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass

from ..backends.tool_ir import AssistantTurn, ToolResult
from ..conversation.active_turn_compact import CarriedToolCompactSource
from ..conversation.compact_guard import ConversationCompactError
from ..conversation.compact_tool_identity import (
    compact_tool_ref,
    compact_tool_ref_key,
    compact_tool_refs,
)
from ..tooling.runtime_contracts import ToolCall, ToolContentBlock, ToolResultRef

# 与 tool_call_archive_record._projection_refs 里“完整原始输出”那条引用同源：归档自己写下的输出位置字段。
# 两边必须一起改，test_compact_tool_partition 有同步测试。
_ARCHIVE_OWN_REF_FIELDS = ("output_path", "artifact_ref", "source_artifact_ref")


# LLM: 每组只属于一条原AssistantTurn到下一AssistantTurn的时间区间；refs保留原顺序，complete不从归档补缺失ToolResult。
# 类用途: 保存一组工具调用与回执的只读分析，供分区和来源验证共用。
@dataclass(frozen=True)
class _IRGroup:
    positions: tuple[int, ...]
    refs: tuple[dict[str, str], ...]
    call_names: tuple[tuple[str, str], ...]
    unknown_call_names: tuple[tuple[str, str], ...]
    result_names: tuple[tuple[str, str], ...]
    paired: bool
    complete: bool


# LLM: 只返回完整IR来源的原ToolCall四元refs；retained模式跳过未知身份，不能据结果call_id补造执行轮身份。
#   archive_refs 是同一次分区的“四元键→原归档记录引用值”，完整性判定必须和分区时用同一份（来源对象由自身 source_records 重算）。
# 函数用途: 给来源对象验证和保留区去重共用同一IR分组判据，不修改输入历史。
def recovery_ir_tool_refs(
    history: Sequence[object],
    *,
    require_complete: bool = False,
    archive_refs: Mapping[object, frozenset[str]] | None = None,
) -> tuple[dict[str, str], ...]:
    if not isinstance(history, (list, tuple)) or type(require_complete) is not bool:
        raise TypeError("原生工具历史或完整性参数无效")
    groups = _analyze_ir_groups(history, archive_refs)
    if require_complete:
        positions = {position for group in groups for position in group.positions}
        keys = [compact_tool_ref_key(ref) for group in groups for ref in group.refs]
        if (not all(group.complete for group in groups)
                or len(positions) != len(history)
                or len(keys) != len(set(keys))):
            raise ConversationCompactError("原生工具来源不能证明完整配对", code="COMPACT_TOOL_COVERAGE_UNKNOWN")
    return compact_tool_refs([ref for group in groups for ref in group.refs])


# LLM: 保留IR可以夹杂事实或未完成调用；仅确证一对一且全局无歧义的旧组可免于重复生成archive handoff。
# 函数用途: 只读列出已完整配对的原生工具四元身份，不要求该组正文可被摘要覆盖。
def recovery_complete_ir_tool_refs(history: Sequence[object]) -> tuple[dict[str, str], ...]:
    if not isinstance(history, (list, tuple)):
        raise TypeError("原生工具历史必须是序列")
    groups = _analyze_ir_groups(history)
    counts = Counter(compact_tool_ref_key(ref) for group in groups for ref in group.refs)
    orphan_names = _orphan_result_names(history, groups)
    return compact_tool_refs([
        ref for group in groups if group.paired
        and not any(name in orphan_names for name in group.call_names)
        and all(counts[compact_tool_ref_key(item)] == 1 for item in group.refs)
        for ref in group.refs
    ])


# LLM: 归档优先并入精确refs，IR仅整组可替代；任何重复、已覆盖或未证明组都不进入source，也不按裸call_id删记录。
# 函数用途: 从原归档和原生历史划出可摘要工具区及保持原时间位置的保留区，结果与入参深度隔离。
def partition_recovery_tool_source(
    records: Sequence[dict[str, object]],
    history: Sequence[object],
    *,
    covered_tool_refs: Sequence[object] = (),
) -> CarriedToolCompactSource | None:
    if (not isinstance(records, (list, tuple)) or any(not isinstance(item, dict) for item in records)
            or not isinstance(history, (list, tuple)) or not isinstance(covered_tool_refs, (list, tuple))):
        raise TypeError("工具压缩分区需要原归档、原生历史和覆盖引用序列")
    covered = {compact_tool_ref_key(ref) for ref in compact_tool_refs(list(covered_tool_refs))}
    base_groups = _analyze_ir_groups(history)
    archive_keys = [compact_tool_ref_key(item) for item in records]
    ambiguous = _ambiguous_keys(archive_keys, [compact_tool_ref_key(ref) for group in base_groups for ref in group.refs])
    orphan_names = _orphan_result_names(history, base_groups)
    unknown_names = {name for group in base_groups for name in group.unknown_call_names}
    archive_refs = _proof_archive_refs(records, archive_keys, covered | ambiguous, orphan_names | unknown_names)
    groups = _analyze_ir_groups(history, archive_refs)
    source_groups = tuple(group for group in groups if group.complete
                          and not any(name in orphan_names for name in group.call_names)
                          and all(compact_tool_ref_key(ref) not in covered | ambiguous for ref in group.refs))
    source_positions = {position for group in source_groups for position in group.positions}
    retained_groups = tuple(group for group in groups if group not in source_groups)
    blocked_keys = {compact_tool_ref_key(ref) for group in retained_groups for ref in group.refs}
    blocked_names = orphan_names | {name for group in retained_groups for name in group.unknown_call_names}
    source_records = tuple(item for item, key in zip(records, archive_keys) if key is not None
                           and key not in covered | ambiguous | blocked_keys
                           and _record_name(item) not in blocked_names)
    source_archive_ids = {id(item) for item in source_records}
    retained_records = tuple(item for item in records if id(item) not in source_archive_ids)
    source_ir = tuple(item for index, item in enumerate(history) if index in source_positions)
    if not source_records and not source_ir:
        return None
    retained_ir = tuple(item for index, item in enumerate(history) if index not in source_positions)
    source_refs = compact_tool_refs([
        *source_records, *recovery_ir_tool_refs(source_ir, require_complete=True, archive_refs=archive_refs),
    ])
    retained_refs = compact_tool_refs([
        *(item for item in retained_records if compact_tool_ref_key(item) is not None),
        *recovery_ir_tool_refs(retained_ir),
    ])
    return CarriedToolCompactSource(
        source_records=deepcopy(source_records), retained_records=deepcopy(retained_records),
        source_tool_refs=source_refs, retained_tool_refs=retained_refs,
        source_ir_history=deepcopy(source_ir), retained_ir_history=deepcopy(retained_ir),
    )


# LLM: 同一四元ref各在archive和IR出现一次合法；任一侧自身重复，或两侧合计超过两次，都不再提供可覆盖证明。
# 函数用途: 找出归档与原生历史里身份重复、不能证明覆盖的四元键，供分区整体排除。
def _ambiguous_keys(archive_keys: Sequence[object], ir_keys: Sequence[object]) -> set[object]:
    ambiguous = {key for key, count in Counter([*archive_keys, *ir_keys]).items() if key is not None and count > 2}
    ambiguous.update(key for key, count in Counter(key for key in archive_keys if key is not None).items() if count > 1)
    ambiguous.update(key for key, count in Counter(key for key in ir_keys if key is not None).items() if count > 1)
    return ambiguous


# LLM: 能给带引用回执作证的归档只取身份完整、不在排除键（已覆盖/歧义）里、名字不属孤儿或未知调用的记录；
#   这些记录必然随其组进入 source_records，CarriedToolCompactSource 复核时能从 source_records 重算出同一份。
# 函数用途: 为分区挑出能证明“回执引用由归档保存”的原归档记录，并取出它们写下的输出位置。
def _proof_archive_refs(
    records: Sequence[Mapping[str, object]],
    archive_keys: Sequence[object],
    excluded_keys: set[object],
    excluded_names: set[tuple[str, str]],
) -> dict[object, frozenset[str]]:
    return archive_ref_values_by_key(
        item for item, key in zip(records, archive_keys)
        if key is not None and key not in excluded_keys and _record_name(item) not in excluded_names)


# LLM: 只读原归档记录自己写下的输出位置字段（外置全文路径、分页读取的来源归档或来源逻辑引用），不读文件、不按引用类型猜。
#   read_artifact 的来源引用指向别的调用，引用上的 sha256/size_bytes 描述的是本次读取回执，这里只比对引用值本身。
#   工具在结果信封里自报的引用（记录顶层 tool_result_refs 只是回执引用的副本）不算：归档不为它们保存内容，这类回执保留原位。
#   重复四元键在分区前已判为歧义，这里后出现的记录覆盖先出现的只影响已被排除的键。
# 函数用途: 为“带引用的回执能否移入摘要来源”给出每个四元身份在归档侧写下的输出位置；来源对象复核也复用它。
def archive_ref_values_by_key(records: Iterable[Mapping[str, object]]) -> dict[object, frozenset[str]]:
    result: dict[object, frozenset[str]] = {}
    for record in records:
        key = compact_tool_ref_key(record)
        if key is None:
            continue
        values = {str(record.get(field) or "").strip() for field in _ARCHIVE_OWN_REF_FIELDS}
        values.discard("")
        result[key] = frozenset(values)
    return result


# 函数用途: 取原归档记录的 (call_id, 工具名)，供孤儿/未知调用名字比对。
def _record_name(record: Mapping[str, object]) -> tuple[str, str]:
    return str(record.get("call_id") or ""), str(record.get("tool") or record.get("tool_name") or "")


# LLM: AssistantTurn时间区间是结果归属唯一边界；孤儿结果及其它IR项不加入任何可替代组。archive_refs 为空时带引用回执一律不完整。
# 函数用途: 按原顺序把每条模型工具轮和其后、下一模型轮前的回执交给同一个判据检查。
def _analyze_ir_groups(
    history: Sequence[object], archive_refs: Mapping[object, frozenset[str]] | None = None,
) -> tuple[_IRGroup, ...]:
    starts = [index for index, item in enumerate(history) if isinstance(item, AssistantTurn)]
    return tuple(_analyze_ir_group(history, start, starts[index + 1] if index + 1 < len(starts) else len(history),
                                   archive_refs or {})
                 for index, start in enumerate(starts))


# LLM: 完整性同时要求精确身份、call_id/tool_name一对一、无夹杂记录及可摘要正文；不借archive填缺失的IR回执，
#   只用同一四元身份原归档写下的输出位置证明回执里的引用都由归档保存。
# 函数用途: 判断一条AssistantTurn和时间区间里的全部ToolResult能否作为完整来源移动。
def _analyze_ir_group(
    history: Sequence[object], start: int, end: int, archive_refs: Mapping[object, frozenset[str]],
) -> _IRGroup:
    turn = history[start]
    calls = list(turn.tool_calls) if isinstance(turn.tool_calls, (list, tuple)) else []
    refs = tuple(ref for call in calls if isinstance(call, ToolCall) and (ref := compact_tool_ref(call)) is not None)
    names = tuple(_tool_call_name(call) for call in calls)
    unknown_names = tuple(name for call, name in zip(calls, names)
                          if not isinstance(call, ToolCall) or compact_tool_ref(call) is None)
    result_positions = [index for index in range(start + 1, end) if isinstance(history[index], ToolResult)]
    results = [history[index] for index in result_positions]
    result_names = [(item.call_id, item.tool_name) for item in results]
    last_result = result_positions[-1] if result_positions else start
    paired = (
        bool(calls) and len(refs) == len(calls)
        and all(isinstance(call, ToolCall) for call in calls)
        and len(set(names)) == len(names)
        and len({compact_tool_ref_key(ref) for ref in refs}) == len(refs)
        and all(isinstance(history[index], ToolResult) for index in range(start + 1, last_result + 1))
        and Counter(names) == Counter(result_names)
    )
    keys_by_name = {_tool_call_name(call): compact_tool_ref_key(ref) for call in calls
                    if isinstance(call, ToolCall) and (ref := compact_tool_ref(call)) is not None}
    complete = paired and _assistant_blocks_text_complete(turn) and all(
        _result_blocks_text_complete(item, archive_refs.get(keys_by_name.get((item.call_id, item.tool_name))))
        for item in results)
    return _IRGroup((start, *result_positions), refs, names, unknown_names, tuple(result_names), paired, complete)


# LLM: 非ToolCall形状只能用于保守关联未读archive，绝不能提升为IR来源四元身份。
# 函数用途: 从未知旧调用记录读取名字以阻止误覆盖，真正可摘要组仍要求原ToolCall对象。
def _tool_call_name(call: object) -> tuple[str, str]:
    if isinstance(call, Mapping):
        return str(call.get("call_id") or ""), str(call.get("tool_name") or call.get("tool") or "")
    return str(getattr(call, "call_id", "") or ""), str(getattr(call, "tool_name", "") or "")


# LLM: 无原AssistantTurn或同区间无匹配ToolCall的回执不能补造四元身份；同名archive和完整组一律保守保留。
# 函数用途: 找出无法归属的结果名字，防止从孤儿结果旁的归档记录获得覆盖权。
def _orphan_result_names(history: Sequence[object], groups: tuple[_IRGroup, ...]) -> set[tuple[str, str]]:
    paired_positions = {position for group in groups for position in group.positions}
    names = {(item.call_id, item.tool_name) for index, item in enumerate(history)
             if isinstance(item, ToolResult) and index not in paired_positions}
    for group in groups:
        names.update(name for name in group.result_names if name not in group.call_names)
    return names


# LLM: 原adapter只认可明确文字及匹配的canonical工具块；未知/不透明块即使有摘要文本也不证明原文完整。
# 函数用途: 检查模型assistant正文和回放块可完整纳入摘要，遇到媒体或新增未知格式保守保留。
def _assistant_blocks_text_complete(turn: AssistantTurn) -> bool:
    if (not isinstance(turn.text, str) or not isinstance(turn.content_blocks, (list, tuple))
            or not isinstance(turn.tool_calls, (list, tuple))):
        return False
    calls = {call.call_id: call for call in turn.tool_calls if isinstance(call, ToolCall)}
    return all(_assistant_block_text_complete(block, calls) for block in turn.content_blocks)


# LLM: 回放适配器只认可明确文字、thinking及canonical工具块；未来未知块仍可保留在IR，但不能授予压缩覆盖权。
# 函数用途: 核对一块assistant原生内容与同轮真实ToolCall一致且没有未读的不透明字段。
def _assistant_block_text_complete(block: object, calls: dict[str, ToolCall]) -> bool:
    if not isinstance(block, dict):
        return False
    kind = block.get("type")
    if kind == "tool_use":
        call = calls.get(block.get("id"))
        return (call is not None and block.get("name", call.tool_name) == call.tool_name
                and ("input" not in block or block["input"] == call.arguments)
                and not set(block) - {"type", "id", "name", "input"})
    if kind == "text":
        return isinstance(block.get("text"), str) and not set(block) - {"type", "text"}
    if kind == "thinking":
        return (isinstance(block.get("thinking"), str)
                and ("signature" not in block or isinstance(block["signature"], str))
                and not set(block) - {"type", "thinking", "signature"})
    return False


# LLM: ToolResult原合同可有ref/json/媒体；只有文本块与引用块、且每个引用都等于同一四元身份原归档写下的输出位置
#   （archived_refs；没有归档记录时为 None）才可移入摘要来源。媒体引用、json/数据块、工具自报或对不上的引用一律保留。
# 函数用途: 媒体和工具自报引用照旧留在原位，同时让归档保存了全文或来源的普通文字回执可以被摘要替代。
def _result_blocks_text_complete(result: ToolResult, archived_refs: frozenset[str] | None = None) -> bool:
    ref_values: set[str] = set()
    for ref in result.refs:
        mime = str(getattr(ref, "mime_type", "") or "")
        if (archived_refs is None or not isinstance(ref, ToolResultRef) or ref.ref not in archived_refs
                or (mime and not mime.startswith("text/"))):
            return False
        ref_values.add(ref.ref)
    for block in result.content_blocks:
        if (not isinstance(block, ToolContentBlock) or block.data is not None
                or (block.mime_type and not block.mime_type.startswith("text/"))):
            return False
        if block.type == "ref" and block.ref in ref_values:
            continue
        if block.type != "text" or block.ref:
            return False
    return True


__all__ = [
    "archive_ref_values_by_key",
    "partition_recovery_tool_source",
    "recovery_complete_ir_tool_refs",
    "recovery_ir_tool_refs",
]
