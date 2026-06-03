
from __future__ import annotations

"""normalizes orchestration-tool parameters and one-shot tool-call guard keys.

模型传来的工具参数可能是数组、JSON 字符串、多行文本、逗号分隔文本。
这个文件专门把这些输入整理成稳定类型，也负责拦住同一轮重复创建任务的一次性编排工具。
"""

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
