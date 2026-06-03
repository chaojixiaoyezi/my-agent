
from __future__ import annotations

from ..runtime.live_archive import archive_assistant_tool_round_if_enabled
from ..tool_context.call_reducer import (
    AssistantToolRoundContextRequest,
    render_assistant_tool_round_context,
)


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
