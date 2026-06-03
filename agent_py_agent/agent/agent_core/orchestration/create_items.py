
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ..runner.ref_fields import (
    _file_refs_from_value,
    _normalize_file_ref,
    params_output_refs,
)

_BASE_FIELDS_EXCLUDED_FROM_ITEM = {
    "count",
    "items",
    "goal",
    "plan",
    "context_manifest",
    # Top-level delivery targets belong to the parent/root output. In batch mode
    # each child keeps its own explicit output refs; shared files are coordinated
    # by the parent prompt/tree/closeout instead of a hidden create-time gate.
    "output_files",
    "output_refs",
    "artifact_refs",
    "required_output_files",
    "required_output_refs",
    "deliverables",
    "final_output",
    "final_output_path",
}
_SHARED_DIRECTIVE_ROLES = frozenset(
    {
        "primary_directive",
        "directive",
        "instruction",
        "brief",
    }
)


@dataclass(frozen=True)
class CreateSubagentItem:
    goal: str
    params: dict[str, object]


def create_items_from_params(params: dict[str, object]) -> list[CreateSubagentItem] | str:
    protocol_error = _batch_protocol_error(params)
    if protocol_error:
        return protocol_error
    raw_items = params.get("items")
    if raw_items is None:
        return []
    items = _json_list_param(raw_items)
    if not items:
        return "items 必须是包含 goal 的对象列表。"
    parsed: list[CreateSubagentItem] = []
    for index, raw in enumerate(items, start=1):
        item = _create_item(params, raw, index)
        if isinstance(item, str):
            return item
        parsed.append(item)
    return parsed


def _batch_protocol_error(params: dict[str, object]) -> str:
    if "tasks" in params:
        return "create_subagents 批量派工只接受 items；请把 tasks 改成 items。"
    for key in ("replaces_run_ids", "supersedes_run_ids"):
        if key in params:
            return f"create_subagents 接管关系只接受 replacement_for_run_ids；请移除 {key}。"
    raw_items = params.get("items")
    item_error = _item_protocol_error(raw_items)
    if item_error:
        return item_error
    return ""


def _item_protocol_error(raw_items: object) -> str:
    items = _json_list_param(raw_items) if raw_items is not None else []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        bad_key = _old_replacement_key(item)
        if bad_key:
            return f"items[{index}] 接管关系只接受 replacement_for_run_ids；请移除 {bad_key}。"
    return ""


def _old_replacement_key(params: dict[str, object]) -> str:
    for key in ("replaces_run_ids", "supersedes_run_ids"):
        if key in params:
            return key
    return ""


def _create_item(
    base_params: dict[str, object],
    raw: object,
    index: int,
) -> CreateSubagentItem | str:
    if not isinstance(raw, dict):
        return f"items[{index}] 必须是对象。"
    goal = str(raw.get("goal") or "").strip()
    if not goal:
        return f"items[{index}] 缺少必填 goal。"
    merged = _create_item_params(base_params, raw, goal)
    return CreateSubagentItem(goal=goal, params=merged)


def _create_item_params(
    base_params: dict[str, object],
    raw: dict[str, object],
    goal: str,
) -> dict[str, object]:
    merged = _base_item_defaults(base_params)
    merged["_item_allowed_tools_explicit"] = "allowed_tools" in raw
    merged.update(raw)
    merged["goal"] = goal
    merged["count"] = 1
    _merge_item_required_read_paths(merged, base_params, goal)
    return merged


# 不自动变成每个子代理的硬输入依赖，避免一个来源清单卡住所有 worker。
def _base_item_defaults(base_params: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in base_params.items()
        if key not in _BASE_FIELDS_EXCLUDED_FROM_ITEM
    }


# 顶层 required_read_paths 不自动复制到每个 worker，避免把一组输入清单误变成所有子代理的读提示。
def _merge_item_required_read_paths(
    merged: dict[str, object],
    base_params: dict[str, object],
    goal: str,
) -> None:
    refs = _merge_refs(
        [
            _shared_directive_pack_paths(base_params.get("context_packs")),
            string_list(merged.get("required_read_paths"), TOOL_TEXT_LIST_OPTIONS),
            _item_manifest_required_read_paths(merged.get("context_manifest")),
            _existing_goal_file_refs(goal, output_refs=params_output_refs(merged)),
        ]
    )
    if refs:
        merged["required_read_paths"] = refs


def _item_manifest_required_read_paths(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    return string_list(value.get("required_read_paths"), TOOL_TEXT_LIST_OPTIONS)


def _shared_directive_pack_paths(value: object) -> list[str]:
    packs = _context_pack_list(value)
    paths: list[str] = []
    for pack in packs:
        role = str(pack.get("role") or pack.get("kind") or "").strip().lower()
        if role not in _SHARED_DIRECTIVE_ROLES:
            continue
        paths.extend(string_list(pack.get("path") or pack.get("ref"), TOOL_TEXT_LIST_OPTIONS))
    return paths


def _existing_goal_file_refs(goal: str, *, output_refs: list[str]) -> list[str]:
    refs = []
    for ref in _file_refs_from_value(goal):
        normalized = _normalize_file_ref(ref)
        if not normalized or _ref_matches_any_output(normalized, output_refs):
            continue
        if _ref_exists_now(normalized):
            refs.append(normalized)
    return refs


def _context_pack_list(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [dict(value)]
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _merge_refs(groups: list[list[str]]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            _append_merged_ref(merged, seen, item)
    return merged


def _append_merged_ref(merged: list[str], seen: set[str], item: object) -> None:
    normalized = _normalize_file_ref(item)
    if not normalized or normalized in seen:
        return
    seen.add(normalized)
    merged.append(normalized)


def _ref_exists_now(ref: str) -> bool:
    path = Path(ref).expanduser()
    if path.is_absolute():
        return path.exists()
    return Path(ref).exists()


def _ref_matches_any_output(ref: str, output_refs: list[str]) -> bool:
    return any(_path_ref_matches(ref, output_ref) for output_ref in output_refs)


def _path_ref_matches(left: str, right: str) -> bool:
    left_text = str(left or "").strip().replace("\\", "/")
    right_text = str(right or "").strip().replace("\\", "/")
    if not left_text or not right_text:
        return False
    return (
        left_text == right_text
        or left_text.endswith("/" + right_text)
        or right_text.endswith("/" + left_text)
    )


def _json_list_param(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return []
