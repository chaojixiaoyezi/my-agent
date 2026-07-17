
from __future__ import annotations

"""原生 tool_use 协议下的 IR 历史维护（Step 2 接线层）。

这一层把工具循环运行时已经产出的「调用 payload + 执行结果」翻译进 Step 1 的结构化
IR（``AssistantTurn`` / ``ToolResult`` / ``UserTurn``），追加到
``params.tool_ir_history`` 上，再由 ``AnthropicMessageAdapter`` 翻成厂商原生
``messages``。它与现有 ``tool_context: list[str]``
文本链路**共存**（灰度双轨）——只有 ``native_tool_use_active(agent)`` 为真时才写 IR，
text 协议路径一字不动。

历史项序列（与 ``message_adapter.HistoryItem`` 对齐）：

    AssistantTurn(text=该轮模型文本, tool_calls=[ToolCall, ...])
    ToolResult, ToolResult, ...        # 紧随其后、与上面调用一一配对的回执
    UserTurn(text=运行中补充输入)       # 留在到达时的准确时间位置

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

为什么 content 用「结构化结果的精简文本」而不是原始 ``output`` 全文：tool_result 的
``content`` 仍是字符串，必须既保留模型可读的结果，又不把几十 KB 正文塞回 messages。
这里复用与 text 链路同源的 ``render_tool_result_for_live_prompt``（外置时只留摘要+恢复
锚点，内联时保留有界正文），保证 IR 与文本两轨的「给模型看到的结果」口径一致。
"""

from typing import Any

from ..backends.tool_ir import AssistantTurn, ToolCall, ToolResult, UserTurn


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


def open_assistant_turn_ir(params: object, *, tool_rounds: int, response_text: str) -> None:
    """为「这一轮」开一条 AssistantTurn 并落定其可见文本（每轮调用一次）。

    在本轮第一个工具结果落历史前调用，保证 assistant 文本来自该轮真实
    ``ModelResponse.text``。同一轮内多个工具调用随后由 ``record_tool_call_ir`` 追加进
    这条 turn 的 ``tool_calls``。
    """
    _ensure_assistant_turn(native_tool_ir_history(params), tool_rounds, response_text)


def record_tool_call_ir(
    params: object,
    *,
    tool_rounds: int,
    payload: object,
    call_id: str,
    result_content: str,
    is_error: bool,
) -> None:
    """把一次工具调用的「调用 + 结果」追加进 IR 历史（native 专用）。

    ``call_id`` 必须是真实 provider tool_use id（native 下由 ``payload["call_id"]``
    或结果侧 ``ToolExecutionResult.call_id`` 提供），出站 tool_result 的 ``tool_use_id``
    才能与 assistant 的 ``tool_use.id`` 配对。本轮的 AssistantTurn 通常已由
    ``open_assistant_turn_ir`` 开好；若没开（防御）这里会以空文本补开。
    """
    history = native_tool_ir_history(params)
    turn = _ensure_assistant_turn(history, tool_rounds, "")
    call = ToolCall.from_payload(payload, fallback_id=call_id)
    turn.tool_calls.append(call)
    history.append(
        ToolResult(
            tool_call_id=call.id,
            content=result_content,
            is_error=is_error,
        )
    )


def drop_tool_call_pairs(params: object, call_ids: set[str]) -> int:
    """按 tool_use id 集合「整对」摘除 ToolCall 与配对的 ToolResult。

    Step 3/4 的 compact 在丢弃最老工具往返时调用这里，保证 assistant 消息里不留下
    没有对应 tool_result 的孤儿 tool_use（Anthropic 会拒绝），也不留下指向已删
    tool_use 的孤儿 tool_result。返回实际摘除的「对」数（以 ToolResult 计）。
    """
    if not call_ids:
        return 0
    history = native_tool_ir_history(params)
    removed = sum(
        1 for item in history if isinstance(item, ToolResult) and item.tool_call_id in call_ids
    )
    rebuilt = [_rewritten_history_item(item, call_ids) for item in history]
    history[:] = [
        item
        for item in rebuilt
        if item is not None and not _is_empty_assistant_turn(item)
    ]
    return removed


def _rewritten_history_item(item: Any, call_ids: set[str]) -> Any:
    """摘除命中的 ToolResult（返回 None）；AssistantTurn 删掉命中的 ToolCall；其余原样。"""
    if isinstance(item, ToolResult):
        return None if item.tool_call_id in call_ids else item
    if isinstance(item, AssistantTurn):
        return _assistant_turn_without(item, call_ids)
    return item


def _ensure_assistant_turn(
    history: list[Any], tool_rounds: int, response_text: str
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
        return turn
    turn = _MarkedAssistantTurn(text=str(response_text or ""), _tool_round=tool_rounds)
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


def _assistant_turn_without(turn: AssistantTurn, call_ids: set[str]) -> AssistantTurn:
    kept = [call for call in turn.tool_calls if call.id not in call_ids]
    if len(kept) == len(turn.tool_calls):
        return turn
    if isinstance(turn, _MarkedAssistantTurn):
        return _MarkedAssistantTurn(text=turn.text, tool_calls=kept, _tool_round=turn._tool_round)
    return AssistantTurn(text=turn.text, tool_calls=kept)


def _is_empty_assistant_turn(item: Any) -> bool:
    return isinstance(item, AssistantTurn) and not item.text and not item.tool_calls


class _MarkedAssistantTurn(AssistantTurn):
    """带轮号标记的 AssistantTurn，仅供本模块按 tool_rounds 归并同轮调用。

    ``AssistantTurn`` 是 frozen dataclass；这里用普通子类承载一个非冻结的轮号属性，
    适配器只读 ``text`` / ``tool_calls``，对它一视同仁，不感知这个标记。
    """

    __slots__ = ("_tool_round",)

    def __init__(self, *, text: str = "", tool_calls: list[ToolCall] | None = None, _tool_round: int = 0) -> None:
        object.__setattr__(self, "text", str(text or ""))
        object.__setattr__(self, "tool_calls", list(tool_calls or []))
        object.__setattr__(self, "_tool_round", int(_tool_round))


__all__ = [
    "drop_tool_call_pairs",
    "native_tool_ir_history",
    "open_assistant_turn_ir",
    "record_tool_call_ir",
]
