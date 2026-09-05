
# LLM: 原生工具回收复用唯一 typed IR 重写入口；摘要覆盖事实同时控制旧 assistant 与运行快照回收。
# 模块用途: 按完整请求预算整对整理工具历史，保留配对、用户输入与当前状态，不写另一份 Compact 账。
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

- ``window``（``window_tool_context_for_live_prompt``）：超预算时保留近期、归档其余。
  native 下对应 ``compact_native_ir_to_token_budget``——复用完整 provider 请求的统一
  token 估算，从最旧的工具往返开始整对摘除，直到当前输入落进预算。工具参数与结果
  都由同一估算器计入，不再维护一份漏算 ``tool_use.input`` 的字符口径。
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
from collections.abc import Callable
from typing import Any

from ..backends.tool_ir import AssistantTurn
from ..tooling.runtime_contracts import ToolResult
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


# LLM: The caller supplies the single full-request token estimator. Ordinary windowing preserves
# assistant turns and the newest pair; a caller holding a complete replacement summary may release
# both the final pair and fully covered tool-bearing assistant turns and stale runtime facts in the
# retired prefix. The newest facts and all UserTurn items are never candidates.
# 函数用途: 逐对回收最旧工具往返；完整摘要覆盖时一起回收旧思考和状态，保留用户原话与当前状态。
def compact_native_ir_to_token_budget(
    params: object,
    *,
    max_tokens: int,
    token_estimator: Callable[[], int],
    preserve_newest_pair: bool = True,
    drop_completed_tool_turns: bool = False,
) -> int:
    """按完整 provider 可见 token 预算从最旧开始整对回收 native 工具历史。

    预算必须由调用方使用统一的完整请求估算器提供，不能只数 ``ToolResult.content``：
    ``write_file``、``edit_file`` 等工具的大参数同样会原样进入 ``tool_use.input``，漏算
    它们会让 preflight 在长任务中突然重启当前 turn。这里仅负责按时间整对删除，
    ``ToolCall`` 与 ``ToolResult`` 永远同进同退；当前 turn 的 ``UserTurn`` 不在删除集合。

    默认至少保留最新一对工具往返。只有上层已经把完整旧历史写入替代摘要时，才可显式传
    ``preserve_newest_pair=False``，让单条巨型最新回执也能被摘要替换；并显式传
    ``drop_completed_tool_turns=True``，才能删除这些工具所属、已被摘要覆盖的旧 assistant
    正文及连续退休前缀中的旧运行状态，最新状态始终保留。返回实际删除的配对数。
    估算器即使因取整暂时没有下降，循环也只遍历有限的调用，不会卡死。
    """
    if max_tokens <= 0:
        return 0
    ordered_ids = _ordered_tool_call_ids(native_tool_ir_history(params))
    if not ordered_ids or _nonnegative_estimate(token_estimator) <= max_tokens:
        return 0
    candidates = ordered_ids[:-1] if preserve_newest_pair else ordered_ids
    removed = 0
    for call_id in candidates:
        if _nonnegative_estimate(token_estimator) <= max_tokens:
            break
        removed += drop_tool_call_pairs(
            params,
            {call_id},
            drop_completed_tool_turns=drop_completed_tool_turns,
        )
    return removed


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
        call.call_id
        for position, call in enumerate(item.tool_calls, start=1)
        if (int(round_no), position) in wanted and call.call_id
    }


def _ordered_tool_call_ids(history: list[Any]) -> list[str]:
    """IR 历史里全部工具往返的 tool_use id，按时间（最旧在前）排列。

    以 ``ToolResult`` 出现顺序为准——它紧随其发起调用，等价于工具往返的时间序。
    """
    return [
        item.call_id
        for item in history
        if isinstance(item, ToolResult) and item.call_id
    ]


# LLM: Estimator failures are programming errors and must propagate; returning zero would silently
# skip compaction and re-enter the Gateway active-turn restart loop.
# 函数用途: 读取内部 token 估算并限制为非负数，估算器损坏时直接暴露错误。
def _nonnegative_estimate(estimator: Callable[[], int]) -> int:
    # 估算器是内部 typed callback；若它坏了必须暴露真实程序错误，不能伪装成 0 后
    # 跳过窗口化、再让 provider overflow 路线反复重启当前 turn。
    return max(0, int(estimator()))


__all__ = [
    "compact_native_ir_to_token_budget",
    "reclaim_oldest_native_ir_pairs",
    "tool_use_ids_for_tool_records",
]
