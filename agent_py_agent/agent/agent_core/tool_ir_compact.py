
from __future__ import annotations

"""native 模式下 compact 对结构化 IR 历史的「整对」回收（Step 3）。

为什么要单独一层：text 协议的 compact 四件套（``microcompact`` / ``ptl_retry`` /
``window`` / ``_compression_service``）都操作 ``tool_context: list[str]`` 文本——它们
为省 token **故意「留调用头、挖掉结果正文」**。这种「半摘」在文本协议下无害（文本协议
容忍残缺），但在 native 下 IR 会被翻成 Anthropic 原生 messages：assistant 的
``tool_use`` block 必须有配对的 ``tool_result``。只挖结果正文 = 留下「有 tool_use 无
tool_result」的孤儿 → Anthropic 直接 HTTP 400。

所以 native 下 compact 对 IR 的操作粒度必须是「**整对**」：丢一个工具往返就同时丢它的
``ToolCall``（assistant 侧）和配对 ``ToolResult``（user 侧），由
``tool_ir_history.drop_tool_call_pairs`` 保证无孤儿。本模块把 text compact 的两条
**会真正改变发往 provider 内容**的突变路径，映射成 IR 整对摘除：

- ``window``（``window_tool_context_for_live_prompt``）：超字符预算时保留最近若干、
  归档其余。native 下对应 ``compact_native_ir_to_char_budget``——按同样的「字符预算 +
  保最近」策略，从最旧的工具往返开始整对摘除，直到 IR 估算字符落进预算。
- ``ptl_retry``（``reclaim_oldest_tool_results_for_ptl``）：provider 实报上下文超限时
  丢最旧一批工具结果正文重试。native 下对应 ``reclaim_oldest_native_ir_pairs``——丢
  最旧 fraction 比例（至少 1 对）的工具往返整对，返回摘除对数（0 表示无可摘，调用方
  停止重试落回重量级 compact）。

为什么直接从 IR 自身算丢弃集合、而非解析被回收的文本条目：IR 的 ``ToolResult`` 序列
本身就是「工具结果按时间排列」的权威表达，每个都自带 ``tool_call_id``（真实 provider
tool_use id）。直接对这串 ToolResult 套用「保最近 / 预算」策略，再整对摘除，既不依赖
脆弱的文本解析，又天然 orphan-safe。``tool_use_ids_for_tool_records`` 另外提供「文本
条目 → tool_use id」的映射（按 ``[tool-record round=N index=M]`` 标记 join），供需要
从文本回收联动 IR 的场景与单测使用。

灰度红线：本模块只在 ``native_tool_use_active`` 为真时被调用；text 协议的 compact 行为
一字不动。
"""

import re
from typing import Any

from ..backends.tool_ir import AssistantTurn, ToolResult
from .tool_ir_history import drop_tool_call_pairs, native_tool_ir_history

# 与 _tool_loop_service._record_tool_call 写入的文本条目头一致：
# "[tool-record round=N index=M]\n..."。用于「文本条目 → (round,index)」反查。
_TOOL_RECORD_MARKER_RE = re.compile(r"\[tool-record round=(\d+) index=(\d+)\]")


def reclaim_oldest_native_ir_pairs(params: object, *, fraction: float) -> int:
    """PTL 重试：从最旧开始整对摘除 fraction 比例（至少 1 对）的工具往返。

    返回实际摘除的「对」数；0 表示 IR 里已无工具往返可摘（调用方应停止 PTL 重试、
    落回重量级 compact/resume）。与 text 侧 ``reclaim_oldest_tool_results_for_ptl``
    的语义对齐（持久突变、丢最旧），但粒度是 IR 整对而非文本挖正文。
    """
    ordered_ids = _ordered_tool_call_ids(native_tool_ir_history(params))
    if not ordered_ids:
        return 0
    count = max(1, int(len(ordered_ids) * fraction))
    drop_ids = set(ordered_ids[:count])
    return drop_tool_call_pairs(params, drop_ids)


def compact_native_ir_to_char_budget(params: object, *, max_chars: int) -> int:
    """window：IR 估算字符超 ``max_chars`` 时，从最旧整对摘工具往返直到落进预算。

    估算口径与 text ``window`` 同源近似——按 IR 各项渲染文本的字符数累加（assistant
    文本 + 每个 tool_result content）。保留策略也一致：尽量保最近的工具往返，丢最旧。
    返回摘除的对数（0 表示无需 compact 或无可摘）。
    """
    history = native_tool_ir_history(params)
    if max_chars <= 0 or _ir_char_estimate(history) <= max_chars:
        return 0
    ordered_ids = _ordered_tool_call_ids(history)
    if not ordered_ids:
        return 0
    drop_ids: set[str] = set()
    # 从最旧往新逐对加入丢弃集，直到「剩余」估算落进预算或只剩最后一对。
    for call_id in ordered_ids[:-1]:
        if _ir_char_estimate(history, exclude_ids=drop_ids) <= max_chars:
            break
        drop_ids.add(call_id)
    if not drop_ids:
        return 0
    return drop_tool_call_pairs(params, drop_ids)


def tool_use_ids_for_tool_records(history: list[Any], tool_record_entries: list[str]) -> set[str]:
    """把被回收的 ``[tool-record round=N index=M]`` 文本条目映射成 tool_use id 集合。

    供「文本侧 compact 回收了某些条目、需要联动摘除对应 IR 整对」的场景使用。join 键是
    文本条目头里的 ``(round, index)``：IR 里带轮号标记的 ``AssistantTurn`` 的 ``_tool_round``
    即 round；同一轮内工具调用按发起顺序 append（见 ``record_tool_call_ir``），故第 M 个
    调用（1-based）就是 ``turn.tool_calls[M-1]``。

    无法解析、轮号查不到或 index 越界的条目跳过（宁可不摘也不误摘成孤儿——Step 4 sweep
    还会兜底）。
    """
    wanted = _wanted_round_index_markers(tool_record_entries)
    if not wanted:
        return set()
    ids: set[str] = set()
    for item in history:
        ids.update(_marked_turn_ids_in(item, wanted))
    return ids


def _wanted_round_index_markers(tool_record_entries: list[str]) -> set[tuple[int, int]]:
    wanted: set[tuple[int, int]] = set()
    for entry in tool_record_entries:
        match = _TOOL_RECORD_MARKER_RE.search(str(entry or ""))
        if match is not None:
            wanted.add((int(match.group(1)), int(match.group(2))))
    return wanted


def _marked_turn_ids_in(item: Any, wanted: set[tuple[int, int]]) -> set[str]:
    """带轮号标记的 AssistantTurn 里，命中 (round, 第M个调用) 的 tool_use id 集合。"""
    round_no = getattr(item, "_tool_round", None)
    if not isinstance(item, AssistantTurn) or round_no is None:
        return set()
    return {
        call.id
        for position, call in enumerate(item.tool_calls, start=1)
        if (int(round_no), position) in wanted and call.id
    }


def _ordered_tool_call_ids(history: list[Any]) -> list[str]:
    """IR 历史里全部工具往返的 tool_use id，按时间（最旧在前）排列。

    以 ``ToolResult`` 出现顺序为准——它紧随其发起调用，等价于工具往返的时间序。
    """
    return [
        item.tool_call_id
        for item in history
        if isinstance(item, ToolResult) and item.tool_call_id
    ]


def _ir_char_estimate(history: list[Any], *, exclude_ids: set[str] | None = None) -> int:
    """估算 IR 历史翻成 messages 后的字符量；可排除某些 tool_use id 的往返。

    口径：assistant 文本长度 + 每个未排除 ToolResult 的 content 长度。粗略但单调，
    足以驱动「丢到预算内」的循环（与 text window 的 ``_context_chars`` 同量级近似）。
    """
    excluded = exclude_ids or set()
    return sum(_item_char_estimate(item, excluded) for item in history)


def _item_char_estimate(item: Any, excluded: set[str]) -> int:
    if isinstance(item, ToolResult):
        return 0 if item.tool_call_id in excluded else len(str(item.content or ""))
    if isinstance(item, AssistantTurn):
        return len(str(item.text or ""))
    return 0


__all__ = [
    "compact_native_ir_to_char_budget",
    "reclaim_oldest_native_ir_pairs",
    "tool_use_ids_for_tool_records",
]
