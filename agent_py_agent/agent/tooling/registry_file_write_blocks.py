
from __future__ import annotations

import json
import re
from typing import Any

from .content_transport_policy import RECOVERY_WRITE_CHUNK_CHARS
from .registry_payload_normalize import parse_error_payload

_WRITE_FILE_BLOCK_RE = re.compile(
    r"^[ \t]*\[WRITE_FILE_RAW(?P<attrs>[^\]]*)\](?P<content>.*?)^[ \t]*\[/WRITE_FILE_RAW\]",
    re.DOTALL | re.MULTILINE,
)
_RAW_BLOCK_MARKERS = ("WRITE_FILE_RAW",)
_ATTR_RE = re.compile(
    r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*)\s*=\s*"
    r"(?:\"(?P<double>(?:\\.|[^\"\\])*)\"|'(?P<single>(?:\\.|[^'\\])*)'|(?P<bare>[^\s\]]+))"
)


def parse_write_file_raw_blocks(text: str) -> list[tuple[int, dict[str, Any]]]:
    calls: list[tuple[int, dict[str, Any]]] = []
    for match in _WRITE_FILE_BLOCK_RE.finditer(text):
        attrs = _parse_attrs(match.group("attrs"))
        error = _write_attrs_error(attrs)
        if error:
            calls.append((match.start(), parse_error_payload(error, match.group(0), error_code="WRITE_FILE_RAW_INVALID")))
            continue
        calls.append((match.start(), _write_file_payload(attrs, match.group("content"))))
    return calls


def write_file_raw_block_ranges(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _WRITE_FILE_BLOCK_RE.finditer(text)]


def malformed_file_write_raw_block_calls(text: str) -> list[tuple[int, dict[str, Any]]]:
    valid_ranges = write_file_raw_block_ranges(text)
    calls: list[tuple[int, dict[str, Any]]] = []
    for marker in _RAW_BLOCK_MARKERS:
        calls.extend(_malformed_raw_marker_calls(text, marker, valid_ranges))
    return sorted(calls, key=lambda item: item[0])


def _parse_attrs(raw: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw):
        value = match.group("double")
        if value is None:
            value = match.group("single")
        if value is None:
            value = match.group("bare") or ""
        attrs[match.group("key")] = _decode_attr_value(value)
    return attrs


def _decode_attr_value(value: str) -> str:
    try:
        return str(json.loads(f'"{value}"'))
    except json.JSONDecodeError:
        return value


def _write_attrs_error(attrs: dict[str, str]) -> str:
    if not str(attrs.get("path") or "").strip():
        return "WRITE_FILE_RAW 缺少结构化属性: path"
    return ""


def _write_file_payload(attrs: dict[str, str], content: str) -> dict[str, Any]:
    payload = {
        "tool": "write_file",
        "path": attrs["path"],
        "content": _block_content(content),
    }
    if mode := str(attrs.get("mode") or "").strip():
        payload["mode"] = mode
    return payload


def _block_content(content: str) -> str:
    if content.startswith("\n"):
        content = content[1:]
    if content.endswith("\n"):
        content = content[:-1]
    return content

def _position_in_ranges(pos: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in ranges)


def _malformed_raw_marker_calls(
    text: str,
    marker: str,
    valid_ranges: list[tuple[int, int]],
) -> list[tuple[int, dict[str, Any]]]:
    opener = f"[{marker}"
    calls: list[tuple[int, dict[str, Any]]] = []
    for pos in _raw_marker_positions(text, opener):
        if _position_in_ranges(pos, valid_ranges) or not _looks_like_raw_block_opener(text, pos, opener):
            continue
        sample = _raw_block_sample(text, pos)
        payload = parse_error_payload(
            _malformed_raw_block_error(marker),
            sample,
            error_code="WRITE_FILE_RAW_MALFORMED",
        )
        payload.update(_raw_write_recovery_fields(sample))
        calls.append((pos, payload))
    return calls


def _raw_marker_positions(text: str, opener: str) -> list[int]:
    positions: list[int] = []
    cursor = 0
    while True:
        pos = text.find(opener, cursor)
        if pos == -1:
            return positions
        positions.append(pos)
        cursor = pos + len(opener)


def _looks_like_raw_block_opener(text: str, pos: int, opener: str) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    if text[line_start:pos].strip():
        return False
    next_char = text[pos + len(opener) : pos + len(opener) + 1]
    return not next_char or next_char.isspace() or next_char in {"]", ":"}


def _malformed_raw_block_error(marker: str) -> str:
    return f"{marker} 原文块格式错误，缺少完整结构化 header 或结束标记 [/{marker}]"


def _raw_block_sample(text: str, pos: int) -> str:
    candidates = [
        idx + len(marker)
        for marker in ("[/WRITE_FILE_RAW]", "[/TOOL_CALL]")
        if (idx := text.find(marker, pos)) != -1
    ]
    end = min(candidates) if candidates else len(text)
    return text[pos:end].strip()


def _raw_write_recovery_fields(sample: str) -> dict[str, object]:
    header_end = sample.find("]")
    attrs = _parse_attrs(sample[len("[WRITE_FILE_RAW") : header_end if header_end >= 0 else len(sample)])
    path = str(attrs.get("path") or "").strip()
    if not path:
        return {}
    return {
        "source_tool": "WRITE_FILE_RAW",
        "path": path,
        "previous_write_committed": False,
        "write_recovery": {
            "strategy": "restart_same_file_with_append_chunks",
            "path": path,
            "max_chunk_chars": RECOVERY_WRITE_CHUNK_CHARS,
            "first_tool_call": {
                "tool": "write_file",
                "path": path,
                "mode": "overwrite",
                "content": f"<first chunk <= {RECOVERY_WRITE_CHUNK_CHARS} chars>",
            },
            "next_tool_call": {
                "tool": "write_file",
                "path": path,
                "mode": "append",
                "content": f"<next chunk <= {RECOVERY_WRITE_CHUNK_CHARS} chars>",
            },
        },
    }


__all__ = [
    "malformed_file_write_raw_block_calls",
    "parse_write_file_raw_blocks",
    "write_file_raw_block_ranges",
]
