
from __future__ import annotations

"""Extract explicit required content lines from structured contracts."""

import re
from typing import Any

from ..common.value_parsing import dedupe_strings

_REQUIRED_CONTENT_RE = re.compile(
    r"^\s*(?:[-*]\s*)?required_content_(?:lines|texts?)\s*[:=]\s*(?P<tail>.*)$",
    re.IGNORECASE,
)
_REQUIRED_CONTENT_FILE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?required_content_(?:lines|texts?)\[([^\]]+)\]\s*[:=]\s*(?P<tail>.*)$",
    re.IGNORECASE,
)
_ANY_STRUCTURED_FIELD_RE = re.compile(r"^\s*(?:[-*]\s*)?[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?\s*[:=]")
_BULLET_RE = re.compile(r"^\s*[-*]\s*(?P<value>.*)$")


def required_content_lines_from_texts(texts: list[object]) -> list[str]:
    """Return explicit required content lines from structured task text."""

    values: list[str] = []
    for value in texts:
        values.extend(_required_lines_from_text(str(value or "")))
    return dedupe_strings(values)[:100]


def required_content_lines_by_file_from_texts(texts: list[object]) -> dict[str, list[str]]:
    """Return explicit per-file content-line contracts from task text."""

    merged: dict[str, list[str]] = {}
    for value in texts:
        _merge_file_line_mapping(merged, _required_lines_by_file_from_text(str(value or "")))
    return dict(list(merged.items())[:50])


def required_content_lines_for_task(task: Any) -> list[str]:
    """Return explicit content-line contracts from a subagent task."""

    return dedupe_strings(_required_content_line_list(_task_attributes(task).get("required_content_lines")))[:100]


def required_content_lines_by_file_for_task(task: Any) -> dict[str, list[str]]:
    """Return explicit per-file content contracts from a subagent task."""

    return _content_files_from_attributes(_task_attributes(task))


def _task_attributes(task: Any) -> dict[str, Any]:
    attributes = getattr(task, "attributes", {})
    return attributes if isinstance(attributes, dict) else {}


def _content_files_from_attributes(attributes: dict[str, Any]) -> dict[str, list[str]]:
    raw = attributes.get("required_content_files")
    if isinstance(raw, dict):
        return _content_files_from_mapping(raw)
    if isinstance(raw, list):
        return _content_files_from_rows(raw)
    return {}


def _content_files_from_mapping(raw: dict[object, object]) -> dict[str, list[str]]:
    items: dict[str, list[str]] = {}
    for key, value in raw.items():
        file_key = _clean_file_key(key)
        lines = dedupe_strings(_required_content_line_list(value))[:100]
        if file_key and lines:
            items[file_key] = lines
    return dict(list(items.items())[:50])


def _content_files_from_rows(rows: list[object]) -> dict[str, list[str]]:
    items: dict[str, list[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        file_key = _clean_file_key(row.get("path") or row.get("file") or row.get("file_path"))
        lines = dedupe_strings(
            _required_content_line_list(row.get("lines") or row.get("required_content_lines") or row.get("required_lines"))
        )[:100]
        if file_key and lines:
            items[file_key] = lines
    return dict(list(items.items())[:50])


def _required_lines_from_text(text: str) -> list[str]:
    lines = str(text or "").splitlines()
    values: list[str] = []
    index = 0
    while index < len(lines):
        match = _REQUIRED_CONTENT_RE.match(lines[index].strip())
        if not match:
            index += 1
            continue
        extracted, consumed = _content_from_structured_match(match.group("tail"), lines[index + 1 :])
        values.extend(extracted)
        index += consumed + 1
    return values


def _required_lines_by_file_from_text(text: str) -> dict[str, list[str]]:
    lines = str(text or "").splitlines()
    values: dict[str, list[str]] = {}
    index = 0
    while index < len(lines):
        match = _REQUIRED_CONTENT_FILE_RE.match(lines[index].strip())
        if not match:
            index += 1
            continue
        extracted, consumed = _content_from_structured_match(match.group("tail"), lines[index + 1 :])
        _extend_file_lines(values, _clean_file_key(match.group(1)), extracted)
        index += consumed + 1
    return values


def _content_from_structured_match(tail: str, following_lines: list[str]) -> tuple[list[str], int]:
    if tail.strip():
        return _inline_items(tail), 0
    fenced, consumed = _following_fenced_content(following_lines)
    if fenced:
        return fenced, consumed
    consumed, bullet_values = _following_bullet_items(following_lines)
    return bullet_values, consumed


def _following_fenced_content(lines: list[str]) -> tuple[list[str], int]:
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or not lines[start].strip().startswith("```"):
        return [], 0
    values: list[str] = []
    offset = start + 1
    while offset < len(lines):
        stripped = lines[offset].strip()
        if stripped.startswith("```"):
            return [_clean_item(item) for item in values if _clean_item(item)], offset + 1
        values.append(stripped)
        offset += 1
    return [], 0


def _following_bullet_items(lines: list[str]) -> tuple[int, list[str]]:
    values: list[str] = []
    consumed = 0
    for line in lines:
        status, value = _bullet_continuation_value(line, has_values=bool(values))
        if status == "skip":
            consumed += 1
            continue
        if status == "stop":
            break
        if value:
            values.append(value)
        consumed += 1
    return consumed, values


def _bullet_continuation_value(line: str, *, has_values: bool) -> tuple[str, str]:
    if _ANY_STRUCTURED_FIELD_RE.match(line):
        return "stop", ""
    bullet = _BULLET_RE.match(line)
    if bullet:
        return "value", _clean_item(bullet.group("value"))
    if not line.strip() and not has_values:
        return "skip", ""
    return "stop", ""


def _inline_items(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if "|" in text:
        return [_clean_item(item) for item in text.split("|") if _clean_item(item)]
    return [_clean_item(text)] if _clean_item(text) else []


def _merge_file_line_mapping(merged: dict[str, list[str]], mapping: dict[str, list[str]]) -> None:
    for file_key, lines in mapping.items():
        if lines:
            merged[file_key] = dedupe_strings([*merged.get(file_key, []), *lines])[:100]


def _extend_file_lines(values: dict[str, list[str]], file_key: str, lines: list[str]) -> None:
    if not file_key:
        return
    values[file_key] = dedupe_strings([*values.get(file_key, []), *lines])


def _clean_file_key(value: object) -> str:
    return str(value or "").strip().strip("'\"").replace("\\", "/").lstrip("./")


def _clean_item(value: object) -> str:
    return str(value or "").strip().strip("`").strip()


def _required_content_line_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return [_clean_item(item) for item in value if _clean_item(item)]
    text = _clean_item(value)
    return [text] if text else []
