# LLM: Tool-call guardrails stop repeated identical tool failures without reading task prose.
# 模块用途: 记录同一 run 内工具名+结构化参数的失败次数，连续失败时返回机器阻断结果，避免工具循环空转。

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..tooling import ToolExecutionResult

_STATE_ATTR = "_tool_call_guardrail_failures"
_DEFAULT_EXACT_FAILURE_BLOCK_AFTER = 3
_BLOCK_CODE = "TOOL_REPEATED_EXACT_FAILURE"


@dataclass(frozen=True)
class ToolCallSignature:
    tool_name: str
    args_hash: str
    scope: str

    def key(self) -> tuple[str, str, str]:
        return (self.scope, self.tool_name, self.args_hash)


def maybe_block_repeated_tool_failure(agent: object, params: object, payload: dict[str, object]):
    signature = _signature(params, payload)
    if not signature.tool_name:
        return None
    failures = _failure_state(agent).get(signature.key(), 0)
    if failures < _block_after(params):
        return None
    return ToolExecutionResult(
        signature.tool_name,
        False,
        json.dumps(
            {
                "error": "repeated identical tool call failure blocked",
                "guardrail": {
                    "code": _BLOCK_CODE,
                    "action": "block",
                    "tool_name": signature.tool_name,
                    "count": failures,
                    "args_hash": signature.args_hash,
                    "scope": signature.scope,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        error_code=_BLOCK_CODE,
    )


def record_tool_guard_observation(agent: object, params: object, payload: object, result: ToolExecutionResult) -> None:
    if not isinstance(payload, dict):
        return
    signature = _signature(params, payload)
    if not signature.tool_name or result.error_code == _BLOCK_CODE:
        return
    failures = _failure_state(agent)
    key = signature.key()
    if result.ok:
        failures.pop(key, None)
        return
    failures[key] = failures.get(key, 0) + 1


def _failure_state(agent: object) -> dict[tuple[str, str, str], int]:
    state = getattr(agent, _STATE_ATTR, None)
    if not isinstance(state, dict):
        state = {}
        setattr(agent, _STATE_ATTR, state)
    return state


def _signature(params: object, payload: dict[str, object]) -> ToolCallSignature:
    tool_name = str(payload.get("tool") or "").strip()
    return ToolCallSignature(tool_name=tool_name, args_hash=_args_hash(payload), scope=_scope(params))


def _args_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _scope(params: object) -> str:
    for name in ("run_id", "request_id", "task_id"):
        value = str(getattr(params, name, "") or "").strip()
        if value:
            return f"{name}:{value}"
    return "agent"


def _block_after(params: object) -> int:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict):
        value: Any = attrs.get("tool_guard_exact_failure_block_after")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            return parsed
    return _DEFAULT_EXACT_FAILURE_BLOCK_AFTER


__all__ = ["maybe_block_repeated_tool_failure", "record_tool_guard_observation"]
