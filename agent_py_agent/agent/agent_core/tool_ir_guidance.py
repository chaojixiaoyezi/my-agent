
from __future__ import annotations

"""Forward non-tool runtime guidance into native tool-use messages.

Native provider messages already carry tool calls, results, and current-turn
user steering through the structured IR. Policy, progress, and deferred runtime
guidance still lives in ``tool_context``; this module forwards only those non-IR
entries and deduplicates them across rounds.
"""

from typing import Any

# 这些前缀的 tool_context 条目已由 IR 历史（ToolCall/ToolResult/AssistantTurn）承载，
# 不能再作为文本折回 messages（会与原生 tool_use 块双份重复）。
_IR_BACKED_PREFIXES = (
    "[ACTIVE_TURN_USER_INPUT]",
    "[tool-record",
    "[tool-output-record",
    "[assistant-tool-round-",
    "[SUBAGENT_RESULT]",
)


def trailing_runtime_guidance(tool_context: object) -> list[str]:
    """取 ``tool_context`` 尾部「最后一条工具记录之后」的运行时指引条目（按序）。

    返回的是已渲染好的文本块列表（不含工具往返记录）。没有可转发指引时返回 ``[]``。
    """
    if not isinstance(tool_context, list) or not tool_context:
        return []
    tail: list[str] = []
    for item in reversed(tool_context):
        text = str(item or "")
        if not text.strip():
            continue
        if _is_ir_backed_entry(text):
            break
        tail.append(text)
    tail.reverse()
    return tail


def unforwarded_runtime_guidance(tool_context: object, seen: set[str]) -> list[str]:
    """取 ``tool_context`` 里「所有还没转发过」的非工具运行时指引条目（按序）。

    比 :func:`trailing_runtime_guidance` 更宽：不只取尾部连续段，而是扫全表，凡是
    非 IR 承载、且其文本不在 ``seen`` 里的条目都取出来（并登记进 ``seen``）。这样
    **夹在工具往返中间**的指引（护栏 / 进度 / deferred 通知等被后续
    ``[tool-record]`` 越过、再也落不到尾部的那批）也能在 native 下到达
    模型。``seen`` 做精确文本去重，保证每条唯一指引在整个会话里只转发一次，绝不逐轮
    重复堆叠。``seen`` 由调用方跨轮持有（见 ``live_archive_state``）。
    """
    if not isinstance(tool_context, list) or not tool_context:
        return []
    fresh: list[str] = []
    for item in tool_context:
        text = str(item or "")
        if not text.strip():
            continue
        if _is_ir_backed_entry(text):
            continue
        if text in seen:
            continue
        seen.add(text)
        fresh.append(text)
    return fresh


def append_runtime_guidance_user_message(
    messages: list[dict[str, Any]],
    tool_context: object,
    *,
    seen: set[str] | None = None,
) -> list[dict[str, Any]]:
    """把待转发的运行时指引接成一条收尾 ``user`` 文本消息追加到 ``messages``（native 专用）。

    要求 ``messages`` 末尾已是一条 ``user`` tool_result 消息（IR 正常出站形态）——指引
    单独成一条新的 user 文本消息，紧跟其后，不与 tool_result 同条（保持 tool_result
    块纯净，规避 Anthropic 对 user content 混排的边界）。无指引或 messages 为空时原样返回。

    切片口径取决于是否传入 ``seen``：
    - 传入 ``seen``（跨轮去重集合）：走 :func:`unforwarded_runtime_guidance`，转发全表里
      所有未转发过的指引——夹在工具往返中间、被后续工具记录越过的指引也能到达模型；
      靠精确文本去重保证每条只转发一次。这是 native 下避免中段指引丢失的治根口径。
    - 不传 ``seen``（``None``）：退回 :func:`trailing_runtime_guidance` 的纯尾部口径，
      行为与历史一致（仅供无状态调用方/旧测试）。
    """
    guidance = (
        unforwarded_runtime_guidance(tool_context, seen)
        if seen is not None
        else trailing_runtime_guidance(tool_context)
    )
    if not guidance or not messages:
        return messages
    text = "\n\n".join(guidance)
    if not text.strip():
        return messages
    return [*messages, {"role": "user", "content": [{"type": "text", "text": text}]}]


def _is_ir_backed_entry(text: str) -> bool:
    return any(text.startswith(prefix) for prefix in _IR_BACKED_PREFIXES)


__all__ = [
    "append_runtime_guidance_user_message",
    "trailing_runtime_guidance",
    "unforwarded_runtime_guidance",
]
