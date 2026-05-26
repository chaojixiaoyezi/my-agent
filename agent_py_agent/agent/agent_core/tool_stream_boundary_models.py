# LLM: Tool stream boundary models keep abort payloads structured while main parser code stays small.
# 模块用途: 存放流式工具边界使用的异常类和结构化参数，避免主模块同时承载协议逻辑和数据模型。

from __future__ import annotations

from dataclasses import dataclass


# LLM: LongToolContentAbortPayload bundles stream-abort facts so exception init stays compact and stable.
# 类用途: 保存大 write 工具流式中断时需要的结构化字段，供异常和恢复逻辑复用。
@dataclass(frozen=True)
class LongToolContentAbortPayload:
    tool: str
    path: str
    chars: int
    limit: int
    action: str = ""
    session_id: str = ""
    chunk_index: int | None = None
    content_prefix: str = ""


# LLM: LongToolContentStreamAbort is a structured early-stop signal for oversized write tool streams.
# 类用途: 表示模型正在输出过长的 write_file content；上层会把它转成可恢复的分块提示。
class LongToolContentStreamAbort(RuntimeError):
    # LLM: __init__ stores one payload object so the constructor stays below parameter limits.
    # 函数用途: 记录被中断的工具名、路径、已流式输出字符数和上限，方便后续提示模型分块恢复。
    def __init__(self, payload: LongToolContentAbortPayload) -> None:
        super().__init__(
            f"{payload.tool}.content inline content streaming exceeded {payload.limit} chars for {payload.path or '<unknown>'}"
        )
        self.tool = payload.tool
        self.path = payload.path
        self.chars = payload.chars
        self.limit = payload.limit
        self.action = payload.action
        self.session_id = payload.session_id
        self.chunk_index = payload.chunk_index
        self.content_prefix = payload.content_prefix

# LLM: MalformedToolProtocolStreamAbort stops repeated protocol markers before they burn the full request timeout.
# 类用途: 表示模型连续输出未闭合工具调用标记；上层会转成结构化 parse error 让下一轮恢复。
class MalformedToolProtocolStreamAbort(RuntimeError):
    # LLM: __init__ stores marker storm facts without trusting the surrounding model text.
    # 函数用途: 记录异常工具协议标记、出现次数和阈值，方便日志和恢复提示说明问题来源。
    def __init__(self, *, start_marker: str, marker_count: int, limit: int) -> None:
        super().__init__(
            f"tool protocol emitted {marker_count} unclosed {start_marker} markers"
        )
        self.start_marker = start_marker
        self.marker_count = marker_count
        self.limit = limit


# LLM: CompleteToolCallStreamAbort makes the first closed tool block a provider-control boundary.
# 类用途: 表示模型已经流出一个完整可执行工具调用；上层应立即停止继续消费模型输出并执行该工具。
class CompleteToolCallStreamAbort(RuntimeError):
    # LLM: __init__ stores the exact first tool block without trusting any later streamed text.
    # 函数用途: 记录第一个完整工具调用文本和截断位置，供模型调用边界直接返回给工具循环。
    def __init__(self, *, text: str, cut_index: int) -> None:
        super().__init__("complete tool call streamed")
        self.text = text
        self.cut_index = cut_index


__all__ = [
    "CompleteToolCallStreamAbort",
    "LongToolContentAbortPayload",
    "LongToolContentStreamAbort",
    "MalformedToolProtocolStreamAbort",
]
