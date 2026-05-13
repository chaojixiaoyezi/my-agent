# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""normalizes orchestration-tool parameters and one-shot tool-call guard keys.

模型传来的工具参数可能是数组、JSON 字符串、多行文本、逗号分隔文本。
这个文件专门把这些输入整理成稳定类型，也负责拦住同一轮重复创建任务的一次性编排工具。
"""

import json
import time
from pathlib import Path

ONE_SHOT_TOOL_NAMES = {
    "capability_config_patch",
    "create_subagents",
    "schedule_child_subagents",
}


# LLM: _one_shot_tool_call_key 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理oneshot工具callkey相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _one_shot_tool_call_key(payload: dict[str, object]) -> str:

    tool_name = str(payload.get("tool") or "")
    if tool_name not in ONE_SHOT_TOOL_NAMES:
        return ""
    try:
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        normalized = str(sorted((str(key), str(value)) for key, value in payload.items()))
    return f"{tool_name}:{normalized}"

# LLM: _string_list 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理stringlist相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _string_list(value: object) -> list[str]:

    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    if "\n" in text:
        return [line.strip("- ").strip() for line in text.splitlines() if line.strip("- ").strip()]
    if "," in text:
        return [item.strip() for item in text.split(",") if item.strip()]
    return [text]


# LLM: _bool_param 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理boolparam相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _bool_param(value: object, *, default: bool = False) -> bool:
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


# LLM: _positive_int 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理positiveint相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


# LLM: _non_negative_int 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理nonnegativeint相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _non_negative_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


# LLM: _sleep_with_stop 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理sleepstop相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _sleep_with_stop(interval: float, stop_path: Path | None) -> bool:

    if interval <= 0:
        return bool(stop_path and stop_path.exists())
    deadline = time.time() + interval
    while time.time() < deadline:
        if stop_path and stop_path.exists():
            return True
        time.sleep(min(1.0, max(0.0, deadline - time.time())))
    return bool(stop_path and stop_path.exists())
