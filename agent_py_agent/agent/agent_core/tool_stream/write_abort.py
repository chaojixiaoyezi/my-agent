
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ...tooling.content_transport_policy import streaming_inline_write_abort_limit

_WRITE_TOOL_NAMES = {"write_file"}
_JSON_TOOL_RE = re.compile(r'"tool"\s*:\s*"(?P<tool>write_file)"')
_JSON_PATH_RE = re.compile(r'"(?:path|target_path)"\s*:\s*"(?P<path>(?:\\.|[^"\\]){0,240})"')
_JSON_CONTENT_RE = re.compile(r'"content"\s*:\s*"')


@dataclass(frozen=True)
class LongToolContentAbortPayload:
    tool: str
    path: str
    chars: int
    limit: int


class LongToolContentStreamAbort(RuntimeError):
    def __init__(self, payload: LongToolContentAbortPayload) -> None:
        super().__init__(
            f"{payload.tool}.content inline content streaming exceeded {payload.limit} chars for {payload.path or '<unknown>'}"
        )
        self.tool = payload.tool
        self.path = payload.path
        self.chars = payload.chars
        self.limit = payload.limit


def long_write_stream_abort(
    text: str,
    *,
    max_chars: int | None,
    start_info: tuple[int, str] | None,
    first_end_marker: callable,
) -> LongToolContentStreamAbort | None:
    if start_info is None:
        return None
    start, marker = start_info
    if first_end_marker(start + len(marker)) is not None:
        return None
    raw = text[start + len(marker) :]
    tool = _json_tool(raw)
    if tool not in _WRITE_TOOL_NAMES:
        return None
    limit = _streaming_write_abort_limit(tool, max_chars)
    content_start = _content_value_start(raw)
    if content_start is None:
        return None
    content_chars = _streamed_json_string_chars(raw[content_start:])
    if content_chars <= limit:
        return None
    return LongToolContentStreamAbort(
        LongToolContentAbortPayload(
            tool=tool,
            path=_json_path(raw),
            chars=content_chars,
            limit=limit,
        )
    )


def _streaming_write_abort_limit(_tool: str, max_chars: int | None) -> int:
    return streaming_inline_write_abort_limit(max_chars)


def _json_tool(raw: str) -> str:
    match = _JSON_TOOL_RE.search(raw)
    return match.group("tool") if match is not None else ""


def _content_value_start(raw: str) -> int | None:
    match = _JSON_CONTENT_RE.search(raw)
    return match.end() if match is not None else None


def _streamed_json_string_chars(text: str) -> int:
    count = 0
    escaped = False
    for char in text:
        if escaped:
            count += 1
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            break
        count += 1
    return count


def _json_path(raw: str) -> str:
    match = _JSON_PATH_RE.search(raw)
    if match is None:
        return ""
    try:
        return json.loads(f'"{match.group("path")}"')
    except json.JSONDecodeError:
        return match.group("path")


__all__ = [
    "LongToolContentAbortPayload",
    "LongToolContentStreamAbort",
    "long_write_stream_abort",
]
