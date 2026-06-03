
from __future__ import annotations

from dataclasses import dataclass


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


class LongToolContentStreamAbort(RuntimeError):
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

class MalformedToolProtocolStreamAbort(RuntimeError):
    def __init__(self, *, start_marker: str, marker_count: int, limit: int) -> None:
        super().__init__(
            f"tool protocol emitted {marker_count} unclosed {start_marker} markers"
        )
        self.start_marker = start_marker
        self.marker_count = marker_count
        self.limit = limit


class CompleteToolCallStreamAbort(RuntimeError):
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
