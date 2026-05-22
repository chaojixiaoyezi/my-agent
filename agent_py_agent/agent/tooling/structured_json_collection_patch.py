# LLM: Structured JSON collection patch helpers apply verifier-derived row fixes.
# 模块用途: 为 write_structured_json 提供集合行字段更新能力，避免重写整份 checkpoint 或解析自然语言。

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


def has_collection_item_updates(params: dict[str, Any]) -> bool:
    return bool(collection_item_updates(params))


def collection_item_updated_value(target: Path, params: dict[str, Any]) -> object:
    existing = _read_existing_json(target)
    value = deepcopy(existing)
    for update in collection_item_updates(params):
        _apply_update(
            value,
            update,
            items_path=str(params.get("items_path") or "rows"),
            groups_path=str(params.get("groups_path") or ""),
        )
    return value


def collection_item_updates(params: dict[str, Any]) -> list[dict[str, object]]:
    raw = params.get("collection_item_updates")
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise ValueError("TOOL_INVALID_ARGUMENTS: collection_item_updates 必须是非空数组")
    return [_normalized_update(item) for item in raw]


def _normalized_update(item: object) -> dict[str, object]:
    if not isinstance(item, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: collection_item_updates 每项必须是对象")
    field_path = str(item.get("field_path") or "").strip()
    if not field_path:
        raise ValueError("TOOL_INVALID_ARGUMENTS: collection_item_updates.field_path 不能为空")
    return {
        "item_index": _non_negative_int(item.get("item_index"), "collection_item_updates.item_index"),
        "field_path": field_path,
        "value": item.get("value"),
    }


def _apply_update(
    value: object,
    update: dict[str, object],
    *,
    items_path: str,
    groups_path: str,
) -> None:
    items = _mutable_collection_items(value, items_path=items_path, groups_path=groups_path)
    index = update["item_index"]
    if not isinstance(index, int) or index >= len(items):
        raise ValueError("TOOL_INVALID_ARGUMENTS: collection_item_updates.item_index 超出集合范围")
    item = items[index]
    if not isinstance(item, dict):
        raise ValueError("TOOL_INVALID_ARGUMENTS: collection_item_updates 只能更新对象条目")
    _set_nested_field(item, str(update["field_path"]), update.get("value"))


def _mutable_collection_items(value: object, *, items_path: str, groups_path: str) -> list[object]:
    if groups_path:
        groups = _lookup_mutable_path(value, groups_path)
        if not isinstance(groups, list):
            raise ValueError("TOOL_INVALID_ARGUMENTS: groups_path 没有指向数组")
        return [item for group in groups for item in _items_from_holder(group, items_path)]
    return _items_from_holder(value, items_path)


def _items_from_holder(holder: object, items_path: str) -> list[object]:
    if isinstance(holder, list):
        return holder
    items = _lookup_mutable_path(holder, items_path or "rows")
    if isinstance(items, list):
        return items
    raise ValueError("TOOL_INVALID_ARGUMENTS: items_path 没有指向数组")


def _lookup_mutable_path(value: object, path: str) -> object:
    current = value
    for part in [item for item in path.split(".") if item]:
        if not isinstance(current, dict) or part not in current:
            raise ValueError("TOOL_INVALID_ARGUMENTS: JSON 路径不存在")
        current = current[part]
    return current


def _set_nested_field(item: dict[str, object], field_path: str, value: object) -> None:
    current = item
    parts = [part for part in field_path.split(".") if part]
    if not parts:
        raise ValueError("TOOL_INVALID_ARGUMENTS: field_path 不能为空")
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError("TOOL_INVALID_ARGUMENTS: field_path 中间节点不是对象")
        current = child
    current[parts[-1]] = value


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"TOOL_INVALID_ARGUMENTS: {field} 必须是非负整数")
    try:
        number = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"TOOL_INVALID_ARGUMENTS: {field} 必须是非负整数") from exc
    if number < 0:
        raise ValueError(f"TOOL_INVALID_ARGUMENTS: {field} 必须是非负整数")
    return number


def _read_existing_json(target: Path) -> object:
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("STAGED_JSON_INVALID: 目标 JSON 无法读取或解析") from exc


__all__ = ["collection_item_updated_value", "has_collection_item_updates"]
