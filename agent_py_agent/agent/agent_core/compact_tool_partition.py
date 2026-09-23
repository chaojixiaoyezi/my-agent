# LLM: 原生IR与归档只按完整四元调用及同一AssistantTurn内的结果配对分区；未知正文、媒体、插话和重复身份保留原位。
# 模块用途: 纯计算Compact可替代的完整工具往返，交回原CarriedToolCompactSource，不读取存储或调用摘要器。
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
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
from ..tooling.runtime_contracts import ToolCall, ToolContentBlock


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
# 函数用途: 给来源对象验证和保留区去重共用同一IR分组判据，不修改输入历史。
def recovery_ir_tool_refs(history: Sequence[object], *, require_complete: bool = False) -> tuple[dict[str, str], ...]:
    if not isinstance(history, (list, tuple)) or type(require_complete) is not bool:
        raise TypeError("原生工具历史或完整性参数无效")
    groups = _analyze_ir_groups(history)
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
    groups = _analyze_ir_groups(history)
    archive_keys = [compact_tool_ref_key(item) for item in records]
    ir_keys = [compact_tool_ref_key(ref) for group in groups for ref in group.refs]
    ambiguous = {key for key, count in Counter([*archive_keys, *ir_keys]).items() if key is not None and count > 2}
    # 同一四元ref各在archive和IR出现一次合法；任一侧自身重复则整体不再提供可覆盖证明。
    ambiguous.update(key for key, count in Counter(key for key in archive_keys if key is not None).items() if count > 1)
    ambiguous.update(key for key, count in Counter(key for key in ir_keys if key is not None).items() if count > 1)
    orphan_names = _orphan_result_names(history, groups)
    source_groups = tuple(group for group in groups if group.complete
                          and not any(name in orphan_names for name in group.call_names)
                          and all(compact_tool_ref_key(ref) not in covered | ambiguous for ref in group.refs))
    source_positions = {position for group in source_groups for position in group.positions}
    retained_groups = tuple(group for group in groups if group not in source_groups)
    blocked_keys = {compact_tool_ref_key(ref) for group in retained_groups for ref in group.refs}
    blocked_names = orphan_names | {name for group in retained_groups for name in group.unknown_call_names}
    source_records = tuple(item for item, key in zip(records, archive_keys) if key is not None
                           and key not in covered | ambiguous | blocked_keys
                           and (str(item.get("call_id") or ""), str(item.get("tool") or item.get("tool_name") or "")) not in blocked_names)
    source_archive_ids = {id(item) for item in source_records}
    retained_records = tuple(item for item in records if id(item) not in source_archive_ids)
    source_ir = tuple(item for index, item in enumerate(history) if index in source_positions)
    if not source_records and not source_ir:
        return None
    retained_ir = tuple(item for index, item in enumerate(history) if index not in source_positions)
    source_refs = compact_tool_refs([*source_records, *recovery_ir_tool_refs(source_ir, require_complete=True)])
    retained_refs = compact_tool_refs([
        *(item for item in retained_records if compact_tool_ref_key(item) is not None),
        *recovery_ir_tool_refs(retained_ir),
    ])
    return CarriedToolCompactSource(
        source_records=deepcopy(source_records), retained_records=deepcopy(retained_records),
        source_tool_refs=source_refs, retained_tool_refs=retained_refs,
        source_ir_history=deepcopy(source_ir), retained_ir_history=deepcopy(retained_ir),
    )


# LLM: AssistantTurn时间区间是结果归属唯一边界；孤儿结果及其它IR项不加入任何可替代组。
# 函数用途: 按原顺序把每条模型工具轮和其后、下一模型轮前的回执交给同一个判据检查。
def _analyze_ir_groups(history: Sequence[object]) -> tuple[_IRGroup, ...]:
    starts = [index for index, item in enumerate(history) if isinstance(item, AssistantTurn)]
    return tuple(_analyze_ir_group(history, start, starts[index + 1] if index + 1 < len(starts) else len(history))
                 for index, start in enumerate(starts))


# LLM: 完整性同时要求精确身份、call_id/tool_name一对一、无夹杂记录及可摘要正文；不借archive填IR回执。
# 函数用途: 判断一条AssistantTurn和时间区间里的全部ToolResult能否作为完整来源移动。
def _analyze_ir_group(history: Sequence[object], start: int, end: int) -> _IRGroup:
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
    complete = paired and _assistant_blocks_text_complete(turn) and all(_result_blocks_text_complete(item) for item in results)
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


# LLM: ToolResult原合同可有ref/json/媒体；只有显式文本且无不透明附件时才视作可完整摘要，未知类型只保留。
# 函数用途: 防止把归档预览或媒体引用误当作模型原回执全文。
def _result_blocks_text_complete(result: ToolResult) -> bool:
    if result.refs:
        return False
    for block in result.content_blocks:
        if (not isinstance(block, ToolContentBlock) or block.type != "text" or block.data is not None
                or block.ref or (block.mime_type and not block.mime_type.startswith("text/"))):
            return False
    return True


__all__ = ["partition_recovery_tool_source", "recovery_complete_ir_tool_refs", "recovery_ir_tool_refs"]
