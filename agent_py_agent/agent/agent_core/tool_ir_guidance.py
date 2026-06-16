
from __future__ import annotations

"""native tool_use 下「系统注入的 runtime 指引」回到模型视野（治根回归修复）。

背景（native 回归实锤）：native 协议下，发往 provider 的对话主体是 IR 翻出的
``messages``（assistant tool_use / user tool_result 对），而 ``builder`` 把文本
``tool_context`` 整段旁路（``_transcript_tool_context`` 返回 ``[]``，避免文本+原生
双份重复）。问题是 ``tool_context`` 里**不止**工具往返记录——它还累积了一大类
**系统注入的运行时指引**（closeout 打回的 rework 指令、出口合同的续修指令、
delivery-completion 软提醒、问句逃逸守卫、工具护栏提示……）。这些指引不是工具调用，
不会进 IR 历史（IR 只记真实 ToolCall+ToolResult），于是在 native 下被**双重丢失**：
既不在旁路掉的文本 prompt 里，也不在 IR messages 里——native 模型完全看不到。

实锤后果：弱模型 native 把成品直接写进 output/ 后停手，closeout 判 uncontracted
完成（产物可开即过，本就不验证「任务要求跑测试却没跑」）→ 即便后续任何门想打回，
打回指令也进的是 tool_context，native 模型看不到 → 续修轮变成「蒙着眼重复同一输出」
→ 进展签名不变 → 闸断退出。text 下这些指引每轮折进 ``# Tool Transcript`` 模型都看得到，
所以 text 能被 closeout/出口合同有效引导（也更可能在写完后继续跑测试再交付）。

本模块只做一件事：把 ``tool_context`` 里「模型上一次动作之后系统追加的运行时指引」
抽出来，作为一条**收尾 user 文本消息**接到 native ``messages`` 末尾，让 native 模型
和 text 一样收到系统的引导。纯 native 专用、纯加法：text 协议一字不动。

切片口径——只取「尾部连续的非工具记录条目」：
- 工具往返记录（``[tool-record`` / ``[tool-output-record`` / ``[assistant-tool-round-``）
  与子代理收口（``[SUBAGENT_RESULT]``）已由 IR 承载，跳过，绝不重复折回文本。
- 其余条目 = 系统注入指引。只取「最后一条工具记录之后」的尾部连续段——这正是
  「模型上次动作以来系统新加的指引」（rework/出口/软提醒都落在这里）。模型下一轮
  动作后，这段会落到新工具记录之前，自然不再被取，避免逐轮重复堆叠。
"""

from typing import Any

# 这些前缀的 tool_context 条目已由 IR 历史（ToolCall/ToolResult/AssistantTurn）承载，
# 不能再作为文本折回 messages（会与原生 tool_use 块双份重复）。
_IR_BACKED_PREFIXES = (
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
    **夹在工具往返中间**的指引（soft-hint / 护栏 / 进度 / deferred 通知 / closeout
    打回……被后续 ``[tool-record]`` 越过、再也落不到尾部的那批）也能在 native 下到达
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
