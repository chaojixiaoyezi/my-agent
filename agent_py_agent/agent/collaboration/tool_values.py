# LLM: Small value/result helpers for collaboration tools.
# 模块用途: 整理模型工具参数，并生成统一 ToolExecutionResult JSON。

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from ..tools import ToolExecutionResult

if TYPE_CHECKING:
    from ..core import SimpleAgent


def ok(tool: str, payload: dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(tool, True, json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2))


def error(tool: str, code: str, message: str) -> ToolExecutionResult:
    payload = {"ok": False, "error": code, "message": message}
    return ToolExecutionResult(tool, False, json.dumps(payload, ensure_ascii=False, indent=2))


def dict_value(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def dict_values(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def string_values(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


def float_value(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def deadline_at(agent: SimpleAgent, params: dict[str, object]) -> float:
    absolute = float_value(params.get("deadline_at"))
    if absolute > 0:
        return absolute
    seconds = float_value(params.get("deadline_seconds")) or default_deadline_seconds(agent)
    return time.time() + seconds if seconds > 0 else 0.0


def default_deadline_seconds(agent: SimpleAgent) -> float:
    try:
        config = getattr(agent, "config", None)
        return max(0.0, float(getattr(config, "collaboration_default_deadline_seconds", 120)))
    except (TypeError, ValueError):
        return 120.0


def limit_param(value: object, *, default: int) -> int:
    try:
        return max(0, int(value if value is not None else default))
    except (TypeError, ValueError):
        return default
