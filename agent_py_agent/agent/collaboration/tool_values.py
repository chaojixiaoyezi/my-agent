
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from ..common.value_parsing import (
    dict_value,
    dict_values,
    float_value,
    non_negative_int,
    string_list,
)
from ..tools import ToolExecutionResult

if TYPE_CHECKING:
    from ..core import SimpleAgent


def ok(tool: str, payload: dict[str, Any]) -> ToolExecutionResult:
    return ToolExecutionResult(tool, True, json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2))


def error(tool: str, code: str, message: str, details: dict[str, Any] | None = None) -> ToolExecutionResult:
    payload = {"ok": False, "error": code, "message": message}
    if details:
        payload.update(details)
    return ToolExecutionResult(tool, False, json.dumps(payload, ensure_ascii=False, indent=2))


def string_values(value: object) -> list[str]:
    return string_list(value)


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
    return non_negative_int(value, default=default)
