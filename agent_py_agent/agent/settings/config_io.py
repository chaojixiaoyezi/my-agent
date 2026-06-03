
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


def parse_scalar(value: str) -> Any:
    value = value.strip().strip('"').strip("'")
    if value.startswith("[") and value.endswith("]"):
        parsed = _parse_inline_list(value)
        if parsed is not None:
            return parsed
    if value.startswith("{") and value.endswith("}"):
        parsed = _parse_inline_dict(value)
        if parsed is not None:
            return parsed
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def _parse_inline_list(value: str) -> list[Any] | None:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    return parsed


def _parse_inline_dict(value: str) -> dict[str, Any] | None:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): item for key, item in parsed.items()}


def load_simple_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key: str | None = None
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if _append_yaml_list_item(data, current_key, line):
            continue
        current_key = _handle_yaml_mapping_line(data, current_key, line)
    return data


def _append_yaml_list_item(data: dict[str, Any], current_key: str | None, line: str) -> bool:
    if not (line.startswith("  - ") and current_key):
        return False
    data.setdefault(current_key, []).append(parse_scalar(line[4:]))
    return True


def _handle_yaml_mapping_line(data: dict[str, Any], current_key: str | None, line: str) -> str | None:
    if ":" not in line or line.startswith(" "):
        return current_key
    key, value = line.split(":", 1)
    key = key.strip()
    value = value.strip()
    if value == "":
        data[key] = []
        return key
    data[key] = parse_scalar(value)
    return None
