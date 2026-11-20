
# LLM: 原生工具回收复用唯一 typed IR 重写入口；摘要覆盖事实同时控制旧 assistant 与运行快照回收。
# 模块用途: 形成正常候选和带UTF-8校验的回合降级桩；只采用完整token更小的覆盖，不改原IR或持久状态。
from __future__ import annotations

"""native 模式下 compact 对结构化 IR 历史的「整对」回收（Step 3）。

为什么要单独一层：text 协议的输出窗口与超限回收（``microcompact`` / ``ptl_retry`` /
``window``）操作 ``tool_context: list[str]`` 文本——它们
为省 token **故意「留调用头、挖掉结果正文」**。这种「半摘」在文本协议下无害（文本协议
容忍残缺），但在 native 下 IR 会被翻成 Anthropic 原生 messages：assistant 的
``tool_use`` block 必须有配对的 ``tool_result``。只挖结果正文 = 留下「有 tool_use 无
tool_result」的孤儿 → Anthropic 直接 HTTP 400。

所以 native 下 compact 对 IR 的操作粒度必须是「**整对**」：丢一个工具往返就同时丢它的
``ToolCall``（assistant 侧）和配对 ``ToolResult``（user 侧），由
``tool_ir_history`` 的位置计划保证无孤儿。本模块把 text compact 的两条
**会真正改变发往 provider 内容**的突变路径，映射成 IR 整对摘除：

- ``window``（``window_tool_context_for_live_prompt``）：超预算时保留近期、归档其余。
  native 下对应 ``compact_native_ir_to_token_budget``——复用完整 provider 请求的统一
  token 估算，从最旧的工具往返开始整对摘除，直到当前输入落进预算。工具参数与结果
  都由同一估算器计入，不再维护一份漏算 ``tool_use.input`` 的字符口径。
- ``ptl_retry``（``reclaim_oldest_tool_results_for_ptl``）：provider 实报上下文超限时
  丢最旧一批工具结果正文重试。native 下对应 ``reclaim_oldest_native_ir_pairs``——丢
  最旧 fraction 比例（至少 1 对）的工具往返整对，返回摘除对数（0 表示无可摘，调用方
  停止重试落回重量级 compact）。

回收计划依原回执顺序排列，记录同组调用与回执的位置及原调用的四元身份。
``tool_use_ids_for_tool_records`` 通过同一计划解析旧文本标记，只返回窗口内唯一的工具 ID。

本模块只在 ``native_tool_use_active`` 为真时被调用，不生成摘要或另写 Compact 代次。
旧的记忆拼接 CompressionService 已移除；正式会话摘要由 ConversationStore/checkpoint 链负责。
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..backends.tool_ir import AssistantTurn
from ..common import hot_json
from ..common.fast_dataclass import fast_frozen, fast_replace
from ..conversation.tool_context_window import record_native_ir_window
from ..tooling.runtime_contracts import ToolContentBlock, ToolResult
from .tool_ir_history import (
    _drop_planned_tool_pairs,
    native_tool_ir_history,
    native_tool_result_sources,
    replace_compaction_summary_ir,
    tool_pair_retirement_plan,
)

# 与 _tool_loop_service._record_tool_call 写入的文本条目头一致：
# "[tool-record round=N index=M]\n..."。用于「文本条目 → (round,index)」反查。
_TOOL_RECORD_MARKER_RE = re.compile(r"\[tool-record round=(\d+) index=(\d+)\]")


# LLM: 这里只借用原回合的三个列表；不接收 Agent、Store、权限或提交能力，调用方负责事务回滚。
# 类用途: 给压缩候选提供原历史、窗口提示和归档引用，避免把整个工具循环上下文传进来。
@dataclass(frozen=True)
class NativeCompactWindow:
    tool_ir_history: list[Any]
    tool_context: list[str]
    archive_tool_calls: list[dict]


# LLM: 数字只描述尚未提交的内存候选；不能据此宣称 canonical generation 已更新。
# 类用途: 返回成对回收数量和完整请求重估结果，由原调用方决定提交或恢复。
@fast_frozen
@dataclass(frozen=True, slots=True)
class NativeCompactCandidate:
    dropped_pairs: int
    after_tokens: int


# LLM: 摘要文本可带服务端压缩项密文（compact_remote）：IR 里装的摘要项据此发压缩项而不是文字。
# 类用途: 压缩候选要装进 IR 的摘要：文本 + 可选的压缩项密文。
@dataclass(frozen=True)
class NativeCompactSummary:
    text: str
    provider_compaction: str = ""


# LLM: Only a provider-view override is changed; canonical IR and ToolCall/ToolResult pairs remain
# intact. Overrides use original four-part source identity; force requires fresh shrinkage even
# when a provider rejection contradicts the local estimate. Preserve raw UTF-8 bytes/digest/marker;
# every accepted stub must actually reduce full request tokens. Never invent archive references.
# 函数用途: 保尾四对并折叠旧结果，附原字节校验；无token收益则撤候选，超预算恢复原投影并拒发。
def stub_old_native_tool_results(params: object, *, target_tokens: int, estimate_tokens: Callable[[], int], require_progress: bool = False) -> int:
    from ..conversation.compact_guard import ConversationCompactError
    from ..conversation.compact_progress import COMPACT_AUTHORITY_TURN_LOCAL

    results = native_tool_result_sources(params.tool_ir_history)
    stubs = params.compact_degradation_stubs
    original = dict(stubs)
    changed = 0
    try:
        for source, result in results[:-4]:
            before_tokens = estimate_tokens()
            if before_tokens <= target_tokens and (not require_progress or changed):
                return changed
            if source in stubs:
                continue
            archive_ref = next((ref.ref for ref in result.refs if ref.kind == "artifact"), "")
            archive_ref = archive_ref or next((ref.ref for ref in result.refs), "")
            raw_bytes = result.output.encode("utf-8")
            digest = hashlib.sha256(raw_bytes).hexdigest()
            content = hot_json.dumps({
                "compact_authority": COMPACT_AUTHORITY_TURN_LOCAL,
                "commit_authority": COMPACT_AUTHORITY_TURN_LOCAL,
                "tool_name": result.tool_name, "call_id": result.call_id,
                "status": result.status, "char_length": len(result.output),
                "archive_ref": archive_ref or None,
                "bytes": len(raw_bytes), "sha256": digest,
                "marker": f"[TOOL_OUTPUT_COMPACTED: tool_call_id={result.call_id} bytes={len(raw_bytes)} sha256={digest}]",
            }, ensure_ascii=False, sort_keys=True, compact=True)
            if len(content.encode("utf-8")) >= len(raw_bytes):
                continue
            stub = fast_replace(result, content_blocks=(ToolContentBlock("text", text=content),))
            # 真实结果已有宿主模型投影；只换正文会继续发送旧投影，使折叠计量永远没有进展。
            stubs[source] = stub.with_live_prompt_projection(stub.render_for_prompt())
            if estimate_tokens() >= before_tokens:
                stubs.pop(source)
                continue
            changed += 1
        if estimate_tokens() > target_tokens:
            raise ConversationCompactError("保留当前输入与最近四对工具后仍超过安全预算", code="COMPACT_BUDGET_EXCEEDED")
        return changed
    except BaseException:
        stubs.clear()
        stubs.update(original)
        raise


# LLM: 修改原列表而不持久化；先回收、安装摘要，再把窗口提示计入同一估算器。异常由调用方恢复快照。
#   summary 既接受纯文本（旧调用方/测试），也接受 NativeCompactSummary（带压缩项密文）。
# 函数用途: 形成配对完整的压缩候选；有完整摘要时允许继续回收最后一对，始终保留用户原话与当前事实。
def reduce_native_compact_candidate(
    window: NativeCompactWindow,
    *,
    summary: str | NativeCompactSummary,
    target_tokens: int,
    estimate_tokens: Callable[[], int],
) -> NativeCompactCandidate:
    text = summary.text if isinstance(summary, NativeCompactSummary) else str(summary or "")
    provider_compaction = summary.provider_compaction if isinstance(summary, NativeCompactSummary) else ""
    dropped = compact_native_ir_to_token_budget(
        window,
        max_tokens=max(1, target_tokens),
        token_estimator=estimate_tokens,
        drop_completed_tool_turns=bool(text),
    )
    if not dropped:
        return NativeCompactCandidate(dropped_pairs=0, after_tokens=0)
    if text:
        replace_compaction_summary_ir(window, text, provider_compaction=provider_compaction)
    while True:
        record_native_ir_window(
            window,
            omitted_count=dropped,
            preserved_count=sum(isinstance(item, ToolResult) for item in window.tool_ir_history),
        )
        if estimate_tokens() <= target_tokens:
            break
        additional = compact_native_ir_to_token_budget(
            window,
            max_tokens=max(1, target_tokens),
            token_estimator=estimate_tokens,
            preserve_newest_pair=not bool(text),
            drop_completed_tool_turns=bool(text),
        )
        if additional <= 0:
            break
        dropped += additional
    return NativeCompactCandidate(dropped_pairs=dropped, after_tokens=estimate_tokens())


def reclaim_oldest_native_ir_pairs(params: object, *, fraction: float) -> int:
    """PTL 重试：从最旧开始整对摘除 fraction 比例（至少 1 对）的工具往返。

    返回实际摘除的「对」数；0 表示 IR 里已无工具往返可摘（调用方应停止 PTL 重试、
    落回重量级 compact/resume）。与 text 侧 ``reclaim_oldest_tool_results_for_ptl``
    的语义对齐（持久突变、丢最旧），但粒度是 IR 整对而非文本挖正文。
    """
    history = native_tool_ir_history(params)
    entries = tool_pair_retirement_plan(history).entries
    if not entries:
        return 0
    count = max(1, int(len(entries) * fraction))
    return _drop_planned_tool_pairs(history, entries[:count])


# LLM: The caller supplies the single full-request token estimator. Ordinary windowing preserves
# assistant turns and the newest pair; a caller holding a complete replacement summary may release
# both the final pair and fully covered tool-bearing assistant turns and stale runtime facts in the
# retired prefix. The newest facts and all UserTurn items are never candidates.
# 函数用途: 二分定位满足预算的最短退休前缀，减少重复完整估算；失败恢复原历史，保留用户原话与当前状态。
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
    完整请求估算器必须对退休前缀单调不增。二分寻找最少删除数，取整平台不会卡死；
    每次试探使用同一原历史，估算失败则恢复原历史，不做额外模型请求或落盘。
    """
    if max_tokens <= 0:
        return 0
    history = native_tool_ir_history(params)
    entries = tool_pair_retirement_plan(history).entries
    if not entries or _nonnegative_estimate(token_estimator) <= max_tokens:
        return 0
    candidates = entries[:-1] if preserve_newest_pair else entries
    if not candidates:
        return 0
    original = list(history)
    lower, upper = 1, len(candidates)
    try:
        while lower < upper:
            middle = (lower + upper) // 2
            history[:] = original
            _drop_planned_tool_pairs(
                history, candidates[:middle],
                drop_completed_tool_turns=drop_completed_tool_turns,
            )
            if _nonnegative_estimate(token_estimator) <= max_tokens:
                upper = middle
            else:
                lower = middle + 1
    finally:
        history[:] = original
    return _drop_planned_tool_pairs(
        history,
        candidates[:lower],
            drop_completed_tool_turns=drop_completed_tool_turns,
        )


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
    plan = tool_pair_retirement_plan(history)
    refused = {identity[-1] for identity in plan.non_retirable if not any(identity[:3])}
    complete = {(entry.call_position, entry.identity[-1]) for entry in plan.entries}
    matches: dict[tuple[int, int], tuple[int, str] | None] = {}
    for position, item in enumerate(history):
        if not isinstance(item, AssistantTurn):
            continue
        round_no = getattr(item, "_tool_round", None)
        if round_no is None:
            continue
        for index, call in enumerate(item.tool_calls, start=1):
            marker = (int(round_no), index)
            if marker in wanted:
                matches[marker] = None if marker in matches else (position, call.call_id)
    return {match[-1] for match in matches.values() if match in complete and match[-1] not in refused}


def _wanted_round_index_markers(tool_record_entries: list[str]) -> set[tuple[int, int]]:
    wanted: set[tuple[int, int]] = set()
    for entry in tool_record_entries:
        match = _TOOL_RECORD_MARKER_RE.search(str(entry or ""))
        if match is not None:
            wanted.add((int(match.group(1)), int(match.group(2))))
    return wanted


# LLM: Estimator failures are programming errors and must propagate; returning zero would silently
# skip compaction and re-enter the Gateway active-turn restart loop.
# 函数用途: 读取内部 token 估算并限制为非负数，估算器损坏时直接暴露错误。
def _nonnegative_estimate(estimator: Callable[[], int]) -> int:
    # 估算器是内部 typed callback；若它坏了必须暴露真实程序错误，不能伪装成 0 后
    # 跳过窗口化、再让 provider overflow 路线反复重启当前 turn。
    return max(0, int(estimator()))


__all__ = [
    "NativeCompactWindow",
    "NativeCompactCandidate",
    "reduce_native_compact_candidate",
    "compact_native_ir_to_token_budget",
    "reclaim_oldest_native_ir_pairs",
    "tool_use_ids_for_tool_records",
]
