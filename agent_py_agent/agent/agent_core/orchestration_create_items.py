# LLM: Create-subagents item parsing gives top-level delegation a 长期助手 tasks[] path.
# 模块用途: 解析 create_subagents 的 items/tasks 批量参数，避免 count 复制同一个 goal。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .parameters import _string_list
from .runner_ref_fields import (
    _file_refs_from_value,
    _normalize_file_ref,
    params_output_refs,
)

_BASE_FIELDS_EXCLUDED_FROM_ITEM = {
    "count",
    "items",
    "tasks",
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


# LLM: CreateSubagentItem keeps one requested child task as a structured bundle.
# 类用途: 保存单个顶层子代理的原始参数和目标；调用方再转成 CreateRunParams。
@dataclass(frozen=True)
class CreateSubagentItem:
    goal: str
    params: dict[str, object]


# LLM: create_items_from_params parses batch mode without forcing a top-level goal.
# 函数用途: 从 create_subagents 的 items/tasks 字段解析多个独立子任务；返回字符串表示模型参数错误。
def create_items_from_params(params: dict[str, object]) -> list[CreateSubagentItem] | str:
    protocol_error = _batch_protocol_error(params)
    if protocol_error:
        return protocol_error
    raw_items = params.get("items") if "items" in params else params.get("tasks")
    if raw_items is None:
        return []
    items = _json_list_param(raw_items)
    if not items:
        return "items/tasks 必须是包含 goal 的对象列表。"
    parsed: list[CreateSubagentItem] = []
    for index, raw in enumerate(items, start=1):
        item = _create_item(params, raw, index)
        if isinstance(item, str):
            return item
        parsed.append(item)
    return parsed


# LLM: _batch_protocol_error rejects ambiguous top-level batch envelopes before creating runs.
# 函数用途: 让模型在 create_subagents 入口先修正 items/tasks/count 混用和越层派工，避免创建错误任务树。
def _batch_protocol_error(params: dict[str, object]) -> str:
    if "items" in params and "tasks" in params:
        return "不要同时传 items 和 tasks；二选一即可。"
    raw_items = params.get("items") if "items" in params else params.get("tasks")
    if raw_items is None:
        return ""
    del raw_items
    return ""


# LLM: _create_item merges parent defaults with one explicit child object.
# 函数用途: 让 items[] 的每一项继承顶层验收/工具/写入边界，同时允许 item 自己覆盖。
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


# LLM: _create_item_params keeps only create-run defaults that make sense per child.
# 函数用途: 顶层字段做默认值，item 字段优先；全局 plan/count/items/tasks/goal 不带入单个子任务。
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


# LLM: _base_item_defaults keeps batch-global manifests from becoming per-child blockers.
# 函数用途: items[] 子任务只继承真正通用的顶层字段；全局 context_manifest 的开放字段
# 不自动变成每个子代理的硬输入依赖，避免一个来源清单卡住所有 worker。
def _base_item_defaults(base_params: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in base_params.items()
        if key not in _BASE_FIELDS_EXCLUDED_FROM_ITEM
    }


# LLM: _merge_item_required_read_paths preserves explicit child read refs as hints.
# 函数用途: 只合并当前 item 的显式 read refs、共享指令 pack 和 goal 中已存在文件；
# 顶层 required_read_paths 不自动复制到每个 worker，避免把一组输入清单误变成所有子代理的读提示。
def _merge_item_required_read_paths(
    merged: dict[str, object],
    base_params: dict[str, object],
    goal: str,
) -> None:
    refs = _merge_refs(
        [
            _shared_directive_pack_paths(base_params.get("context_packs")),
            _string_list(merged.get("required_read_paths")),
            _item_manifest_required_read_paths(merged.get("context_manifest")),
            _existing_goal_file_refs(goal, output_refs=params_output_refs(merged)),
        ]
    )
    if refs:
        merged["required_read_paths"] = refs


def _item_manifest_required_read_paths(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    return _string_list(value.get("required_read_paths"))


def _shared_directive_pack_paths(value: object) -> list[str]:
    packs = _context_pack_list(value)
    paths: list[str] = []
    for pack in packs:
        role = str(pack.get("role") or pack.get("kind") or "").strip().lower()
        if role not in _SHARED_DIRECTIVE_ROLES:
            continue
        paths.extend(_string_list(pack.get("path") or pack.get("ref")))
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


# LLM: _json_list_param accepts common LLM encodings while keeping the public API explicit.
# 函数用途: 支持列表、单对象或 JSON 字符串；解析失败时返回空列表让调用方给出可读错误。
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
