
# LLM: 派工幂等键来自结构化参数；显式不同模型必须区分，未选模型的历史键保持原样。
# 模块用途: 规范编排参数与同轮去重身份，不从自然语言猜模型或任务状态。
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
    structured goal/role/replacement facts.  Duplicate work inside one batch is
    rejected by the creation boundary instead of receiving artificial occurrence
    slots.
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
        items = [dict(payload)]
    keys: set[str] = set()
    for item in items:
        identity = subagent_intent_identity(payload, item)
        if not identity:
            continue
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        keys.add(f"create_subagents:intent:{digest}")
    return keys


# LLM: model 与 effort 是显式配置选择，不得被 goal 去重吞掉；省略时不追加字段，保持既有请求键稳定。
# 函数用途: 生成派工去重身份，让同目标的不同模型或不同智能程度对比不会被当成同一个子代理。
def subagent_intent_identity(payload: dict[str, object], item: dict[str, object]) -> str:
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
    work_refs = {
        key: _subagent_intent_list(payload, item, key)
        for key in ("input_refs", "artifact_refs", "output_files", "output_refs", "covers")
    }
    model = item.get("model", payload.get("model"))
    model_ref = {"model": model.strip()} if isinstance(model, str) and model.strip() else {}
    effort = item.get("effort", payload.get("effort"))
    effort_ref = {"effort": effort.strip().lower()} if isinstance(effort, str) and effort.strip() else {}
    return json.dumps(
        {
            "goal": goal,
            "role": role,
            "replacement_for_run_ids": sorted(replacement_ids),
            **work_refs,
            **model_ref,
            **effort_ref,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _subagent_intent_list(
    payload: dict[str, object],
    item: dict[str, object],
    key: str,
) -> list[str]:
    value = item.get(key) if key in item else payload.get(key)
    if isinstance(value, str):
        return sorted(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, list | tuple | set):
        return sorted(str(part).strip() for part in value if str(part).strip())
    return []

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
