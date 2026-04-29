from __future__ import annotations

"""LLM: normalizes orchestration-tool parameters and one-shot tool-call guard keys.

给人看的解释：
模型传来的工具参数可能是数组、JSON 字符串、多行文本、逗号分隔文本。
这个文件专门把这些输入整理成稳定类型，也负责拦住同一轮重复执行的一次性编排工具。
"""

import json
import time
from pathlib import Path

ONE_SHOT_TOOL_NAMES = {"create_subagents", "subagent_board", "dispatch_subagents"}


def _one_shot_tool_call_key(payload: dict[str, object]) -> str:
    """给一次性编排工具生成本轮去重 key。

    真实模型偶尔会在看见工具结果后重复同一个编排工具调用。
    `create_subagents` 多执行一次会多落一个真实工单；`subagent_board`
    重复读虽然不改状态，但会拖慢收口。这里仅在同一轮 `run()` 里拦住
    完全相同的重复调用，保留“以后再次派工/查看”的自由。
    """

    tool_name = str(payload.get("tool") or "")
    if tool_name not in ONE_SHOT_TOOL_NAMES:
        return ""
    try:
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        normalized = str(sorted((str(key), str(value)) for key, value in payload.items()))
    return f"{tool_name}:{normalized}"

def _string_list(value: object) -> list[str]:
    """把工具参数里的数组/JSON 数组/多行文本整理成字符串列表。"""

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


def _positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _non_negative_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _sleep_with_stop(interval: float, stop_path: Path | None) -> bool:
    """睡眠时定期检查 stop 文件；返回 True 表示收到停止请求。"""

    if interval <= 0:
        return bool(stop_path and stop_path.exists())
    deadline = time.time() + interval
    while time.time() < deadline:
        if stop_path and stop_path.exists():
            return True
        time.sleep(min(1.0, max(0.0, deadline - time.time())))
    return bool(stop_path and stop_path.exists())
