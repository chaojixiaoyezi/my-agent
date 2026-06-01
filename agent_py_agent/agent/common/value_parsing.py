# LLM: Shared parsing helpers keep model/tool parameter coercion consistent.
# 模块用途: 提供薄的字符串、布尔、数字和字典解析入口，避免各模块重复写小型 normalize。

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StringListOptions:
    parse_json_list: bool = False
    split_lines: bool = False
    split_commas: bool = False
    strip_bullets: bool = False


TOOL_TEXT_LIST_OPTIONS = StringListOptions(
    parse_json_list=True,
    split_lines=True,
    split_commas=True,
    strip_bullets=True,
)


def string_list(value: object, options: StringListOptions | None = None) -> list[str]:
    options = options or StringListOptions()
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value).strip()
    if not text:
        return []
    parsed = _json_list(text) if options.parse_json_list else None
    if parsed is not None:
        return [str(item).strip() for item in parsed if str(item or "").strip()]
    if options.split_lines and "\n" in text:
        return [_line_item(line, strip_bullets=options.strip_bullets) for line in text.splitlines() if _line_item(line, strip_bullets=options.strip_bullets)]
    if options.split_commas and "," in text:
        return [item.strip() for item in text.split(",") if item.strip()]
    return [text]


def bool_value(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "apply"}:
        return True
    if text in {"0", "false", "no", "n", "off", "dry-run", "dry_run"}:
        return False
    return default


def positive_int(value: object, *, default: int = 0) -> int:
    return max(0, _int_value(value, default=default))


def non_negative_int(value: object, *, default: int = 0) -> int:
    return max(0, _int_value(value, default=default))


def float_value(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def dict_value(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def dict_values(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _json_list(text: str) -> list[object] | None:
    if not text.startswith("["):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _line_item(line: str, *, strip_bullets: bool) -> str:
    text = line.strip()
    return text.strip("- ").strip() if strip_bullets else text


def _int_value(value: object, *, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
