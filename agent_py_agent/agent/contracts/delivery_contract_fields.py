
from __future__ import annotations

from typing import Any

_NAMED_FIELD_KEYS = ("column_name", "name", "field", "key", "label", "title")


def string_items(value: object, *, allow_named_dict: bool = False) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [_string_item(item, allow_named_dict=allow_named_dict) for item in value]
    return [item for item in items if item]


def _string_item(value: object, *, allow_named_dict: bool) -> str:
    if isinstance(value, str):
        return value.strip()
    if not allow_named_dict or not isinstance(value, dict):
        return ""
    return _named_string_item(value)


def _named_string_item(value: dict[str, Any]) -> str:
    for key in _NAMED_FIELD_KEYS:
        item = str(value.get(key) or "").strip()
        if item:
            return item
    return ""
