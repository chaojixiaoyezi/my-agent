# LLM: Create-subagents item parsing gives top-level delegation a Hermes-style tasks[] path.
# 模块用途: 解析 create_subagents 的 items/tasks 批量参数，避免 count 复制同一个 goal。

from __future__ import annotations

import json
from dataclasses import dataclass


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
        return (
            "不要同时传 items 和 tasks；二选一即可。"
            "create_subagents 只创建直接小傻妞；小小傻妞/孙代理请由对应小傻妞在 runner 内"
            "调用 schedule_child_subagents 创建。"
        )
    raw_items = params.get("items") if "items" in params else params.get("tasks")
    if raw_items is None:
        return ""
    count_error = _batch_count_error(params.get("count"))
    if count_error:
        return count_error
    items = _json_list_param(raw_items)
    for index, raw in enumerate(items, start=1):
        if _looks_like_direct_grandchild(raw):
            return (
                f"items[{index}] 越过了一层。create_subagents 只能创建直接小傻妞；"
                "小小傻妞/孙代理必须由对应小傻妞在 runner 内调用 schedule_child_subagents 创建。"
            )
    return ""


# LLM: _batch_count_error keeps count for single-goal mode only.
# 函数用途: 批量 items/tasks 已经逐项表示人数和目标，不再同时使用 count 复制一批。
def _batch_count_error(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text in {"", "1", "1.0"}:
        return ""
    return "items/tasks 批量模式不要同时传 count；每个 item 就是一名直接小傻妞。"


# LLM: _looks_like_direct_grandchild catches model attempts to create lower layers from root.
# 函数用途: 检查 item 的 role/agent_name/name 是否明显写成小小傻妞或 grandchild，防止 root 越层造孙代理。
def _looks_like_direct_grandchild(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    text = "\n".join(
        str(raw.get(key) or "")
        for key in ("role", "agent_name", "name", "display_name")
    ).lower()
    return "grandchild" in text or "小小傻妞" in text


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
    merged = {
        key: value
        for key, value in base_params.items()
        if key not in {"count", "items", "tasks", "goal", "plan"}
    }
    merged.update(raw)
    merged["goal"] = goal
    merged["count"] = 1
    return merged


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
