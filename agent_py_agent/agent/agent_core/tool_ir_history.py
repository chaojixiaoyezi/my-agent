
from __future__ import annotations

"""原生 tool_use 协议下的 IR 历史维护（Step 2 接线层）。

这一层把工具循环运行时已经产出的「调用 payload + 执行结果」翻译进 Step 1 的结构化
IR（``AssistantTurn`` / ``ToolResult`` / ``UserTurn`` / ``CompactionSummary``），追加到
``params.tool_ir_history`` 上，再由 ``AnthropicMessageAdapter`` 翻成厂商原生
``messages``。它与现有 ``tool_context: list[str]``
文本链路**共存**（灰度双轨）——只有 ``native_tool_use_active(agent)`` 为真时才写 IR，
text 协议路径一字不动。

历史项序列（与 ``message_adapter.HistoryItem`` 对齐）：

    AssistantTurn(text=该轮模型文本, tool_calls=[ToolCall, ...])
    ToolResult, ToolResult, ...        # 紧随其后、与上面调用一一配对的回执
    UserTurn(text=运行中补充输入)       # 留在到达时的准确时间位置
    CompactionSummary(text=续接摘要)    # thread/live 摘要最多一条；carried handoff 独立保留

实现要点：
- 每个工具调用先 ``_ensure_assistant_turn`` 拿到/新建「本轮」的 AssistantTurn，把
  ``ToolCall.from_payload`` 追加进它的 ``tool_calls``；再把对应 ``ToolResult`` 追加到
  历史尾。assistant 文本取该轮 ``ModelResponse.text``（首个调用时落定）。
- 「同一轮」用 ``tool_rounds`` 标定：同轮的多个工具调用共享一条 AssistantTurn（对齐
  Anthropic「一条 assistant 消息里多个 tool_use」）。轮号变化即新开一条 AssistantTurn。
- ``ToolResult`` 是 ``frozen`` 不可变值；要为 Step 3/4 的「ToolCall+ToolResult 整对增删」
  铺路，这里提供 ``drop_tool_call_pairs`` 按 ``tool_call_id`` 集合整对摘除（assistant
  turn 里删 ToolCall、历史里删配对的 ToolResult），保持配对不变量。
- ``UserTurn`` 不属于工具结果窗口，工具 compact 不得删除；需要缩短时由上层 active-turn/thread
  compact 处理。
- ``CompactionSummary`` 不属于第二条 compact 路线；thread/live summary 是同一 IR 历史里旧工具
  往返的 replacement item，每次压缩原位替换；带稳定 schema marker 的 carried handoff 是另一条
  当前 turn 交接事实，不能被 replacement 连带删除。

为什么 content 用「结构化结果的精简文本」而不是原始 ``output`` 全文：tool_result 的
``content`` 仍是字符串，必须既保留模型可读的结果，又不把几十 KB 正文塞回 messages。
这里复用与 text 链路同源的 ``render_tool_result_for_live_prompt``（外置时只留摘要+恢复
锚点，内联时保留有界正文），保证 IR 与文本两轨的「给模型看到的结果」口径一致。
"""

from copy import deepcopy
from typing import Any

from ..backends.tool_ir import (
    AssistantTurn,
    CompactionSummary,
    RuntimeFactsTurn,
    UserTurn,
)
from ..tooling.runtime_contracts import ToolCall, ToolResult


def native_tool_ir_history(params: object) -> list[Any]:
    """返回 params 上的 IR 历史 list（缺失时建空 list 并挂回）。"""
    history = getattr(params, "tool_ir_history", None)
    if not isinstance(history, list):
        history = []
        try:
            params.tool_ir_history = history  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            return history
    return history


def record_user_turn_ir(params: object, text: str) -> None:
    """把运行中用户补充输入永久插入当前 turn 的原生消息历史。"""
    content = str(text or "")
    if not content.strip():
        return
    native_tool_ir_history(params).append(UserTurn(content))


# LLM: A dynamic prompt suffix becomes an append-only typed history item before provider
# submission. Exact duplicate facts are emitted once per run; changed facts append at their real
# chronology point and are never moved behind later tool calls on a subsequent request.
# 函数用途: 把本次调用新增的运行事实写入原生历史，并对同一 run 的完全相同事实去重。
def record_runtime_facts_turn_ir(params: object, text: str) -> bool:
    content = str(text or "")
    if not content.strip():
        return False
    state = getattr(params, "live_archive_state", None)
    if isinstance(state, dict):
        seen = state.get("_native_runtime_facts_seen")
        if not isinstance(seen, set):
            seen = set()
            state["_native_runtime_facts_seen"] = seen
        if content in seen:
            return False
        seen.add(content)
    elif any(
        isinstance(item, RuntimeFactsTurn) and item.text == content
        for item in native_tool_ir_history(params)
    ):
        return False
    native_tool_ir_history(params).append(RuntimeFactsTurn(content))
    return True


# LLM: compact 摘要与真实 UserTurn 类型分开；每次安装只替换 thread/live summary，
# schema-marked carried handoff 是当前 turn 的独立事实，必须原位保留到后续压缩代次。
# 函数用途: 在最近工具尾部之前安装唯一一条当前 turn 压缩摘要，并保留运行交接摘要。
def replace_compaction_summary_ir(params: object, text: str) -> bool:
    content = str(text or "").strip()
    if not content:
        return False
    history = native_tool_ir_history(params)
    replacement = CompactionSummary(content)
    rebuilt: list[Any] = []
    installed = False
    for item in history:
        if isinstance(item, CompactionSummary) and not _is_carried_tool_handoff_summary(item):
            if not installed:
                rebuilt.append(replacement)
                installed = True
            continue
        rebuilt.append(item)
    history[:] = rebuilt
    if installed:
        return True
    insert_at = next(
        (
            index
            for index, item in enumerate(history)
            if isinstance(item, (AssistantTurn, ToolResult))
        ),
        len(history),
    )
    history.insert(insert_at, replacement)
    return True


# LLM: Multiple projections still share CompactionSummary for compatibility. The stable schema
# marker, not natural-language content, separates a carried active-turn handoff from replaceable
# thread/live summaries until the IR gains a dedicated typed variant.
# 函数用途: 识别 compact 续跑带入的工具交接摘要，避免第二次压缩把它当旧 thread summary 删除。
def _is_carried_tool_handoff_summary(item: CompactionSummary) -> bool:
    return str(item.text or "").startswith("[active-turn-tool-handoff]")


# LLM: 开轮时要把后端清洗后的有序 content blocks 与可见 text 一起落到同一 AssistantTurn，避免下一工具轮丢失 reasoning 签名。
# 函数用途: 为当前模型轮建立 assistant 历史，并保存后续原生请求需要续接的内部内容块。
def open_assistant_turn_ir(
    params: object,
    *,
    tool_rounds: int,
    response_text: str,
    response_content_blocks: list[dict[str, Any]] | None = None,
) -> None:
    """为「这一轮」开一条 AssistantTurn 并落定其可见文本（每轮调用一次）。

    在本轮第一个工具结果落历史前调用，保证 assistant 文本来自该轮真实
    ``ModelResponse.text``。同一轮内多个工具调用随后由 ``record_tool_call_ir`` 追加进
    这条 turn 的 ``tool_calls``。
    """
    _ensure_assistant_turn(
        native_tool_ir_history(params),
        tool_rounds,
        response_text,
        response_content_blocks,
    )


def record_tool_call_ir(
    params: object,
    *,
    tool_rounds: int,
    call: ToolCall,
    result: ToolResult,
) -> None:
    """Append one canonical call/result pair to native history."""

    if call.call_id != result.call_id or call.tool_name != result.tool_name:
        raise ValueError("canonical tool call/result pair mismatch")
    history = native_tool_ir_history(params)
    turn = _ensure_assistant_turn(history, tool_rounds, "")
    turn.tool_calls.append(call)
    history.append(result)


# LLM: Pair removal is protocol-safe by default; only a caller holding one complete replacement
# summary may also discard an assistant turn whose last tool call was removed.
# 函数用途: 成对删除原生工具调用和结果；已有完整摘要时一并回收对应旧思考轮，避免空壳继续占上下文。
def drop_tool_call_pairs(
    params: object,
    call_ids: set[str],
    *,
    drop_completed_tool_turns: bool = False,
) -> int:
    """按 tool_use id 集合「整对」摘除 ToolCall 与配对的 ToolResult。

    Step 3/4 的 compact 在丢弃最老工具往返时调用这里，保证 assistant 消息里不留下
    没有对应 tool_result 的孤儿 tool_use（Anthropic 会拒绝），也不留下指向已删
    tool_use 的孤儿 tool_result。只有 ``drop_completed_tool_turns=True`` 且调用方已经持有
    完整替代摘要时，才连同已无保留调用的旧 assistant 正文一起删除。返回实际摘除的
    「对」数（以 ToolResult 计）。
    """
    if not call_ids:
        return 0
    history = native_tool_ir_history(params)
    removed = sum(
        1 for item in history if isinstance(item, ToolResult) and item.call_id in call_ids
    )
    rebuilt = [
        _rewritten_history_item(
            item,
            call_ids,
            drop_completed_tool_turns=drop_completed_tool_turns,
        )
        for item in history
    ]
    history[:] = [
        item
        for item in rebuilt
        if item is not None and not _is_empty_assistant_turn(item)
    ]
    return removed


# LLM: This is a structural IR rewrite; prose never decides whether a turn is covered or removable.
# 函数用途: 重写一条原生历史记录，并按调用方的摘要覆盖事实决定是否整轮删除旧 assistant 内容。
def _rewritten_history_item(
    item: Any,
    call_ids: set[str],
    *,
    drop_completed_tool_turns: bool = False,
) -> Any:
    """摘除命中的工具对；完整替代摘要存在时也可删除其所属旧 assistant 轮。"""
    if isinstance(item, ToolResult):
        return None if item.call_id in call_ids else item
    if isinstance(item, AssistantTurn):
        matched = any(call.call_id in call_ids for call in item.tool_calls)
        rewritten = _assistant_turn_without(item, call_ids)
        if drop_completed_tool_turns and matched and not rewritten.tool_calls:
            return None
        return rewritten
    return item


# LLM: 同轮归并只允许补齐先到的真实响应字段；不得用后续工具结果覆盖已经保存的 assistant 块。
# 函数用途: 找到当前轮的 AssistantTurn，必要时新建，或为防御性空 turn 补齐正文和内容块。
def _ensure_assistant_turn(
    history: list[Any],
    tool_rounds: int,
    response_text: str,
    response_content_blocks: list[dict[str, Any]] | None = None,
) -> AssistantTurn:
    """拿到「本轮」的 AssistantTurn；轮号未变且尾项就是它则复用，否则新开一条。

    复用判据是「历史尾部最后一个 AssistantTurn 的轮号 == 当前轮号」。同一轮里第二个
    工具调用追加时，中间已经插了上一调用的 ToolResult，所以这里按「最近的 turn 标记」
    判断，而非「尾项必须是 turn」。
    """
    marker = _last_turn_marker(history)
    if marker is not None and marker[0] == tool_rounds:
        turn = marker[1]
        # 文本以「先到的非空值」为准：open_assistant_turn_ir 通常先带文本开 turn，
        # 后续 record 以空文本复用时不覆盖；反之若 turn 先被空文本补开，这里回填。
        if not turn.text and response_text:
            object.__setattr__(turn, "text", str(response_text))
        if not turn.content_blocks and response_content_blocks:
            object.__setattr__(turn, "content_blocks", deepcopy(response_content_blocks))
        return turn
    turn = _MarkedAssistantTurn(
        text=str(response_text or ""),
        content_blocks=deepcopy(response_content_blocks or []),
        _tool_round=tool_rounds,
    )
    history.append(turn)
    return turn


def _last_turn_marker(history: list[Any]) -> tuple[int, AssistantTurn] | None:
    for item in reversed(history):
        if isinstance(item, _MarkedAssistantTurn):
            return item._tool_round, item
        if isinstance(item, AssistantTurn):
            # 普通 AssistantTurn（无轮号标记）不参与同轮归并，保守新开一条。
            return None
    return None


# LLM: 删除任何 tool_use 都会破坏原厂 thinking 签名与块完整性，因此重写后的 turn 必须退回 text+剩余 canonical calls，不能保留旧 content_blocks。
# 函数用途: 从一轮 assistant 历史中删除指定工具调用，并在发生结构变化时清掉失效的厂商原生块。
def _assistant_turn_without(turn: AssistantTurn, call_ids: set[str]) -> AssistantTurn:
    kept = [call for call in turn.tool_calls if call.call_id not in call_ids]
    if len(kept) == len(turn.tool_calls):
        return turn
    if isinstance(turn, _MarkedAssistantTurn):
        return _MarkedAssistantTurn(
            text=turn.text,
            tool_calls=kept,
            content_blocks=[],
            _tool_round=turn._tool_round,
        )
    return AssistantTurn(text=turn.text, tool_calls=kept, content_blocks=[])


# LLM: thinking-only assistant turn 仍是有效协议历史，不能因没有可见文字或工具调用被 compact 当空项删除。
# 函数用途: 判断 AssistantTurn 是否真的没有正文、调用和可回放内容。
def _is_empty_assistant_turn(item: Any) -> bool:
    return (
        isinstance(item, AssistantTurn)
        and not item.text
        and not item.tool_calls
        and not item.content_blocks
    )


# LLM: 该运行时子类必须完整承载 AssistantTurn 的所有协议字段；新增字段时同步构造器和 compact 重写路径。
# 类用途: 在 AssistantTurn 上附加当前工具轮编号，供同一轮多个调用归并。
class _MarkedAssistantTurn(AssistantTurn):
    """带轮号标记的 AssistantTurn，仅供本模块按 tool_rounds 归并同轮调用。

    ``AssistantTurn`` 是 frozen dataclass；这里用普通子类承载一个非冻结的轮号属性，
    适配器只读 ``text`` / ``tool_calls``，对它一视同仁，不感知这个标记。
    """

    __slots__ = ("_tool_round",)

    def __init__(
        self,
        *,
        text: str = "",
        tool_calls: list[ToolCall] | None = None,
        content_blocks: list[dict[str, Any]] | None = None,
        _tool_round: int = 0,
    ) -> None:
        object.__setattr__(self, "text", str(text or ""))
        object.__setattr__(self, "tool_calls", list(tool_calls or []))
        object.__setattr__(self, "content_blocks", list(content_blocks or []))
        object.__setattr__(self, "_tool_round", int(_tool_round))


__all__ = [
    "drop_tool_call_pairs",
    "native_tool_ir_history",
    "open_assistant_turn_ir",
    "record_runtime_facts_turn_ir",
    "replace_compaction_summary_ir",
    "record_tool_call_ir",
]
