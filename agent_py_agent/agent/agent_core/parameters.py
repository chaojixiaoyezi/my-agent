
from __future__ import annotations

"""normalizes orchestration-tool parameters and one-shot tool-call guard keys.

模型传来的工具参数可能是数组、JSON 字符串、多行文本、逗号分隔文本。
这个文件专门把这些输入整理成稳定类型，也负责拦住同一轮重复创建任务的一次性编排工具。
"""

import hashlib
import json
import time
from pathlib import Path

from ..common.value_parsing import bool_value, non_negative_int, positive_int

ONE_SHOT_TOOL_NAMES = {
    "create_subagents",
    "schedule_child_subagents",
}


def _one_shot_tool_call_key(payload: dict[str, object]) -> str:

    tool_name = str(payload.get("tool") or "")
    if tool_name not in ONE_SHOT_TOOL_NAMES:
        return ""
    try:
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        normalized = str(sorted((str(key), str(value)) for key, value in payload.items()))
    return f"{tool_name}:{normalized}"


def _one_shot_tool_call_keys(payload: dict[str, object]) -> set[str]:
    """Return exact-call and per-child intent keys for one-shot orchestration.

    Native providers may emit one batch create followed by overlapping single-child
    creates in the same assistant response.  The exact payload keys differ, so the
    original guard cannot see the repeated side effects.  Per-child keys use only
    structured goal/role facts and a same-call occurrence slot; an explicit count or
    duplicate item list therefore remains valid on its first execution.
    """
    primary = _one_shot_tool_call_key(payload)
    if not primary:
        return set()
    return {primary, *_create_subagent_intent_keys(payload)}


def _one_shot_tool_call_is_duplicate(payload: dict[str, object], seen: set[str]) -> bool:
    primary = _one_shot_tool_call_key(payload)
    if not primary:
        return False
    if primary in seen:
        return True
    intent_keys = _create_subagent_intent_keys(payload)
    return bool(intent_keys) and intent_keys.issubset(seen)


def _create_subagent_intent_keys(payload: dict[str, object]) -> set[str]:
    if str(payload.get("tool") or "") != "create_subagents":
        return set()
    raw_items = payload.get("items")
    items = [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
    if not items:
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            return set()
        try:
            count = max(1, int(payload.get("count") or 1))
        except (TypeError, ValueError):
            count = 1
        items = [dict(payload) for _ in range(count)]
    occurrences: dict[str, int] = {}
    keys: set[str] = set()
    for item in items:
        identity = _subagent_intent_identity(payload, item)
        if not identity:
            continue
        occurrences[identity] = occurrences.get(identity, 0) + 1
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        keys.add(f"create_subagents:intent:{digest}:{occurrences[identity]}")
    return keys


def _subagent_intent_identity(payload: dict[str, object], item: dict[str, object]) -> str:
    goal = " ".join(str(item.get("goal") or payload.get("goal") or "").split())
    if not goal:
        return ""
    role = str(item.get("role") or payload.get("role") or "").strip().casefold()
    replacement = item.get("replacement_for_run_ids") or payload.get("replacement_for_run_ids") or []
    if isinstance(replacement, str):
        replacement_ids = [part.strip() for part in replacement.split(",") if part.strip()]
    elif isinstance(replacement, list | tuple | set):
        replacement_ids = [str(part).strip() for part in replacement if str(part).strip()]
    else:
        replacement_ids = []
    return json.dumps(
        {"goal": goal, "role": role, "replacement_for_run_ids": sorted(replacement_ids)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

def _bool_param(value: object, *, default: bool = False) -> bool:
    return bool_value(value, default=default)


def _positive_int(value: object, *, default: int) -> int:
    return positive_int(value, default=default)


def _non_negative_int(value: object, *, default: int) -> int:
    return non_negative_int(value, default=default)


def _sleep_with_stop(interval: float, stop_path: Path | None) -> bool:

    if interval <= 0:
        return bool(stop_path and stop_path.exists())
    deadline = time.time() + interval
    while time.time() < deadline:
        if stop_path and stop_path.exists():
            return True
        time.sleep(min(1.0, max(0.0, deadline - time.time())))
    return bool(stop_path and stop_path.exists())
