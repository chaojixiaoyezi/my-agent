# LLM: Assistant tool-round context belongs to prompt compaction/archive plumbing, not execution.
# 模块用途: 渲染并归档一轮模型工具调用摘要，避免大正文反复塞进后续提示词。

from __future__ import annotations

from .runtime_live_archive import archive_assistant_tool_round_if_enabled
from .tool_call_context_reducer import (
    AssistantToolRoundContextRequest,
    render_assistant_tool_round_context,
)


# LLM: append_assistant_tool_round_context protects the next live prompt from large tool payloads.
# 函数用途: 把模型刚生成的工具调用摘要写回 tool_context，并按配置写 live archive。
def append_assistant_tool_round_context(request) -> None:
    rendered = render_assistant_tool_round_context(
        AssistantToolRoundContextRequest(request.response.text, request.calls)
    )
    request.params.tool_context.append(
        f"[assistant-tool-round-{request.tool_rounds}]\n{rendered}"
    )
    archive_assistant_tool_round_if_enabled(
        request.agent,
        request.params,
        tool_round=request.tool_rounds,
        response_text=request.response.text,
        tool_calls=request.calls,
    )


__all__ = ["append_assistant_tool_round_context"]
