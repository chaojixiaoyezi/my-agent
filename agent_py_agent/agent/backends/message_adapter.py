
from __future__ import annotations

"""IR ↔ 厂商原生 messages 的出/入站翻译适配器。

``MessageAdapter`` 是 provider 无关的抽象：
- 出站 ``to_provider_messages``：把 IR 历史（``AssistantTurn`` 与 ``ToolResult``
  批次交替）翻译成某厂商 ``messages`` 数组；
- 入站 ``tool_calls_from_response``：把一次模型响应里的工具调用抽成 ``ToolCall`` IR。

``AnthropicMessageAdapter`` 是 my-agent 唯一需要的实现（只走 anthropic_compatible）。
入站复用 stage1 已经落地的解析：``base.py`` 的 ``_anthropic_tool_use_blocks`` /
``stream_parsers`` 的累积器已经把响应规整成 ``{id,name,input}``，并挂在
``ModelResponse.tool_use_blocks`` 上；这里只是把它们包成 ``ToolCall``，不重复解析。

历史项的类型约定（Step 2 产出、本模块消费）：
- ``AssistantTurn``：一轮 assistant 文本 + 该轮发起的工具调用；
- ``Sequence[ToolResult]`` 或单个 ``ToolResult``：紧随其后的工具回执批次。

本模块纯加法：不修改 ``base.generate`` / ``_tool_loop_service`` / ``builder`` 的现有
文本链路，只新增可被 Step 2 调用的零件。
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Any

from .tool_ir import AssistantTurn, ToolCall, ToolResult

# 历史里一项要么是一轮 assistant（含工具调用），要么是一批工具结果。
HistoryItem = AssistantTurn | ToolResult | Sequence[ToolResult]


class MessageAdapter(ABC):
    """把内部 IR 历史在「provider 原生 messages」之间来回翻译。"""

    @abstractmethod
    def to_provider_messages(self, history: Iterable[HistoryItem]) -> list[dict[str, Any]]:
        """出站：IR 历史 → 厂商原生 ``messages`` 序列。"""

    @abstractmethod
    def tool_calls_from_response(self, response: object) -> list[ToolCall]:
        """入站：一次模型响应 → 本轮的 ``ToolCall`` 列表。"""


class AnthropicMessageAdapter(MessageAdapter):
    """Anthropic ``/v1/messages`` 形态的 IR 适配器。

    出站映射（对齐标杆 长期助手 的 convert_messages 模式）：
    - ``AssistantTurn`` → 一条 ``role="assistant"`` 消息，content 里先放
      ``{"type":"text","text":...}``（仅当文本非空），再依次放每个
      ``{"type":"tool_use","id","name","input"}``；
    - ``ToolResult`` → ``role="user"`` 消息里的
      ``{"type":"tool_result","tool_use_id","content","is_error"}`` block；
    - 连续的 ``ToolResult``（无论来自同一批还是相邻批次）合并进同一条 user 消息——
      Anthropic 要求一轮 tool_use 的所有结果回在一条 user 消息里。
    """

    def to_provider_messages(self, history: Iterable[HistoryItem]) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        # 待合并的连续 tool_result block 缓冲；遇到非结果项时 flush 成一条 user 消息。
        pending_results: list[dict[str, Any]] = []
        for item in history:
            results = _as_tool_results(item)
            if results is not None:
                pending_results.extend(_tool_result_block(result) for result in results)
                continue
            _flush_results(messages, pending_results)
            _append_assistant(messages, item)
        _flush_results(messages, pending_results)
        return messages

    def tool_calls_from_response(self, response: object) -> list[ToolCall]:
        blocks = getattr(response, "tool_use_blocks", None) or []
        return [_tool_call_from_block(block) for block in blocks if isinstance(block, dict)]


def _as_tool_results(item: HistoryItem) -> list[ToolResult] | None:
    """把历史项规整成 ToolResult 列表；非结果项返回 None。"""
    if isinstance(item, ToolResult):
        return [item]
    if isinstance(item, AssistantTurn):
        return None
    if isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
        results = list(item)
        if results and all(isinstance(result, ToolResult) for result in results):
            return results
    return None


def _flush_results(messages: list[dict[str, Any]], pending_results: list[dict[str, Any]]) -> None:
    if not pending_results:
        return
    messages.append({"role": "user", "content": list(pending_results)})
    pending_results.clear()


def _append_assistant(messages: list[dict[str, Any]], item: HistoryItem) -> None:
    if not isinstance(item, AssistantTurn):
        return
    assistant = _assistant_message(item)
    if assistant is not None:
        messages.append(assistant)


def _assistant_message(turn: AssistantTurn) -> dict[str, Any] | None:
    content: list[dict[str, Any]] = []
    if turn.text:
        content.append({"type": "text", "text": turn.text})
    content.extend(_tool_use_block(call) for call in turn.tool_calls)
    if not content:
        return None
    return {"role": "assistant", "content": content}


def _tool_use_block(call: ToolCall) -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": call.id,
        "name": call.name,
        "input": dict(call.input) if isinstance(call.input, dict) else {},
    }


def _tool_result_block(result: ToolResult) -> dict[str, Any]:
    # is_error 始终显式带上：与 IR 字段一一对应，round-trip 可断言；Anthropic 接受
    # 显式的 ``is_error: false``（与省略等价），不改变模型侧语义。
    return {
        "type": "tool_result",
        "tool_use_id": result.tool_call_id,
        "content": result.content,
        "is_error": bool(result.is_error),
    }


def _tool_call_from_block(block: dict[str, Any]) -> ToolCall:
    tool_input = block.get("input")
    return ToolCall(
        id=str(block.get("id", "") or ""),
        name=str(block.get("name", "") or ""),
        input=tool_input if isinstance(tool_input, dict) else {},
    )


__all__ = [
    "AnthropicMessageAdapter",
    "HistoryItem",
    "MessageAdapter",
]
