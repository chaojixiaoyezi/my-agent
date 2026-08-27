
from __future__ import annotations

"""IR ↔ 厂商原生 messages 的出/入站翻译适配器。

``MessageAdapter`` 只负责出站历史翻译。入站响应必须经过
``backends.tool_protocol_adapter``，在那里绑定 run/turn/attempt、Schema hash 和
幂等身份；历史适配器没有这些权威事实，不能自行制造 ``ToolCall``。

``AnthropicMessageAdapter`` 是 my-agent 唯一需要的实现（只走 anthropic_compatible）。
入站复用 stage1 已经落地的解析：``base.py`` 的 ``_anthropic_tool_use_blocks`` /
``stream_parsers`` 的累积器已经把响应规整成 ``{id,name,input}``，并挂在
``ModelResponse.tool_use_blocks`` 上；这里只是把它们包成 ``ToolCall``，不重复解析。

历史项的类型约定（Step 2 产出、本模块消费）：
- ``AssistantTurn``：一轮 assistant 文本 + 该轮发起的工具调用；
- ``Sequence[ToolResult]`` 或单个 ``ToolResult``：紧随其后的工具回执批次。
- ``UserTurn``：用户在同一执行 turn 运行期间追加的 steer，保留其真实时间位置。
- ``CompactionSummary``：替换已回收旧工具往返的非权威续接摘要。

本模块只负责协议翻译，不拥有 compact 策略或状态。
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Any

from .tool_ir import (
    AssistantTurn,
    CompactionSummary,
    RuntimeFactsTurn,
    ToolCall,
    ToolResult,
    UserTurn,
)

# 历史里一项是一轮 assistant、当前 turn 用户输入，或一批工具结果。
HistoryItem = (
    AssistantTurn
    | CompactionSummary
    | RuntimeFactsTurn
    | UserTurn
    | ToolResult
    | Sequence[ToolResult]
)


class MessageAdapter(ABC):
    """把内部 IR 历史在「provider 原生 messages」之间来回翻译。"""

    @abstractmethod
    def to_provider_messages(self, history: Iterable[HistoryItem]) -> list[dict[str, Any]]:
        """出站：IR 历史 → 厂商原生 ``messages`` 序列。"""

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
    - ``UserTurn`` 与 ``CompactionSummary`` → 各自一条 ``role="user"`` 文本消息；
      两者内部类型不同，底座不会把摘要误认成用户 steer。
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
            _append_non_result_message(messages, item)
        _flush_results(messages, pending_results)
        # 注意：这里是「纯翻译」——逐项把 IR 映射成 messages，不做孤儿净化（孤儿净化是
        # 发请求前的最后防线，见 strip_orphaned_tool_blocks，由出站边界
        # _native_provider_messages 调用）。保持本方法纯翻译，便于按片段单测。
        return messages

def _as_tool_results(item: HistoryItem) -> list[ToolResult] | None:
    """把历史项规整成 ToolResult 列表；非结果项返回 None。"""
    if isinstance(item, ToolResult):
        return [item]
    if isinstance(item, AssistantTurn):
        return None
    if isinstance(item, CompactionSummary):
        return None
    if isinstance(item, RuntimeFactsTurn):
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


def _append_non_result_message(messages: list[dict[str, Any]], item: HistoryItem) -> None:
    if isinstance(item, CompactionSummary):
        text = str(item.text or "")
        if text.strip():
            messages.append(
                {"role": "user", "content": [{"type": "text", "text": text}]}
            )
        return
    if isinstance(item, RuntimeFactsTurn):
        text = str(item.text or "")
        if text.strip():
            messages.append(
                {"role": "user", "content": [{"type": "text", "text": text}]}
            )
        return
    if isinstance(item, UserTurn):
        text = str(item.text or "")
        if text.strip():
            messages.append(
                {"role": "user", "content": [{"type": "text", "text": text}]}
            )
        return
    if isinstance(item, AssistantTurn):
        assistant = _assistant_message(item)
        if assistant is not None:
            messages.append(assistant)


# LLM: 有 provider content_blocks 时必须优先按原顺序回放 thinking/text/tool_use；工具块仍从 canonical ToolCall 重建，防止响应侧附加字段或旧参数泄漏。
# 函数用途: 把一轮内部 assistant 历史整理成可安全发送给 Anthropic-compatible 接口的消息。
def _assistant_message(turn: AssistantTurn) -> dict[str, Any] | None:
    content = _replay_assistant_content_blocks(turn)
    if not content:
        if turn.text:
            content.append({"type": "text", "text": turn.text})
        content.extend(_tool_use_block(call) for call in turn.tool_calls)
    if not content:
        return None
    return {"role": "assistant", "content": content}


# LLM: 这是保存块的最后出站白名单；只保留 Anthropic 输入协议允许的字段，并让 canonical ToolCall 成为工具参数唯一事实源。
# 函数用途: 清洗并按原顺序回放 assistant content blocks，同时补齐未出现在保存块里的真实工具调用。
def _replay_assistant_content_blocks(turn: AssistantTurn) -> list[dict[str, Any]]:
    raw_blocks = turn.content_blocks
    if not raw_blocks:
        return []
    calls_by_id = {call.call_id: call for call in turn.tool_calls if call.call_id}
    used_call_ids: set[str] = set()
    replayed: list[dict[str, Any]] = []
    for raw in raw_blocks:
        if not isinstance(raw, dict):
            continue
        block_type = str(raw.get("type") or "")
        if block_type == "text":
            replayed.append({"type": "text", "text": str(raw.get("text") or "")})
            continue
        if block_type == "thinking":
            block = {"type": "thinking", "thinking": str(raw.get("thinking") or "")}
            signature = raw.get("signature")
            if isinstance(signature, str) and signature:
                block["signature"] = signature
            replayed.append(block)
            continue
        if block_type == "redacted_thinking":
            data = raw.get("data")
            if isinstance(data, str) and data:
                replayed.append({"type": "redacted_thinking", "data": data})
            continue
        if block_type != "tool_use":
            continue
        call_id = str(raw.get("id") or "")
        call = calls_by_id.get(call_id)
        if call is None:
            continue
        replayed.append(_tool_use_block(call))
        used_call_ids.add(call_id)
    replayed.extend(
        _tool_use_block(call)
        for call in turn.tool_calls
        if call.call_id not in used_call_ids
    )
    return replayed


def _tool_use_block(call: ToolCall) -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": call.call_id,
        "name": call.tool_name,
        "input": dict(call.arguments),
    }


def _tool_result_block(result: ToolResult) -> dict[str, Any]:
    # is_error 始终显式带上：与 IR 字段一一对应，round-trip 可断言；Anthropic 接受
    # 显式的 ``is_error: false``（与省略等价），不改变模型侧语义。
    return {
        "type": "tool_result",
        "tool_use_id": result.call_id,
        "content": result.render_for_prompt(),
        "is_error": bool(result.is_error),
    }


# tool_use 缺配对结果时补的合成占位（参照标杆 长期助手 _strip_orphaned_tool_blocks /
# 通道运行时 transform-messages：宁可补合成结果也别删 assistant 文本）。
_ORPHAN_TOOL_RESULT_STUB = "[结果已在上下文压缩中回收]"


def strip_orphaned_tool_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """净化出站 messages，保证 tool_use / tool_result 一一配对（Anthropic 强约束）。

    Anthropic ``/v1/messages`` 对孤儿一律 HTTP 400：assistant 的每个 ``tool_use`` 必须在
    后续 user 消息里有同 id 的 ``tool_result``；每个 ``tool_result`` 必须指向前面某个
    ``tool_use``。本函数两类孤儿分别处理：

    - **孤儿 tool_use**（有调用、无配对结果）：**补 stub tool_result**，不删 assistant
      消息——assistant 里可能还带模型文本/推理，删了等于丢内容；补一条
      ``{"type":"tool_result","tool_use_id":id,"content":stub,"is_error":true}`` 更安全。
      stub 紧跟在该 assistant 消息后（新插一条 user 消息，承载该轮所有缺失结果）。
    - **孤儿 tool_result**（结果指向不存在的 tool_use）：**剔除**该 block——没有可补的
      调用头，且这种残留只会触发 400；剔空后该 user 消息若没 block 了，整条删掉。

    纯函数：返回新 list，不改入参。非 native/无工具 block 的 messages 原样返回。
    """
    if not messages:
        return list(messages)
    tool_use_ids = _collect_block_ids(messages, "tool_use", "id")
    result_ids = _collect_block_ids(messages, "tool_result", "tool_use_id")
    swept: list[dict[str, Any]] = []
    for message in messages:
        kept = _message_without_orphan_results(message, tool_use_ids)
        if kept is not None:
            swept.append(kept)
        _append_stub_for_orphan_tool_use(swept, message, result_ids)
    return swept


def _collect_block_ids(messages: list[dict[str, Any]], block_type: str, id_key: str) -> set[str]:
    ids = {
        str(block.get(id_key, "") or "")
        for message in messages
        for block in _content_blocks(message)
        if block.get("type") == block_type
    }
    ids.discard("")
    return ids


def _message_without_orphan_results(
    message: dict[str, Any], tool_use_ids: set[str]
) -> dict[str, Any] | None:
    """剔除指向不存在 tool_use 的 tool_result block；若该消息因此空了返回 None。"""
    blocks = _content_blocks(message)
    if not blocks:
        return message
    kept = [
        block
        for block in blocks
        if not (
            block.get("type") == "tool_result"
            and str(block.get("tool_use_id", "") or "") not in tool_use_ids
        )
    ]
    if len(kept) == len(blocks):
        return message
    if not kept:
        return None
    return {**message, "content": kept}


def _append_stub_for_orphan_tool_use(
    swept: list[dict[str, Any]], message: dict[str, Any], result_ids: set[str]
) -> None:
    """这条 assistant 消息里的 tool_use 若没配对结果，补一条 user(stub tool_result)。"""
    if message.get("role") != "assistant":
        return
    missing = [
        str(block.get("id", "") or "")
        for block in _content_blocks(message)
        if block.get("type") == "tool_use" and str(block.get("id", "") or "") not in result_ids
    ]
    missing = [call_id for call_id in missing if call_id]
    if not missing:
        return
    swept.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": _ORPHAN_TOOL_RESULT_STUB,
                    "is_error": True,
                }
                for call_id in missing
            ],
        }
    )


def _content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


__all__ = [
    "AnthropicMessageAdapter",
    "HistoryItem",
    "MessageAdapter",
    "strip_orphaned_tool_blocks",
]
