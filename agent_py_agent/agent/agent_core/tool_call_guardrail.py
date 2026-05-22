# LLM: Tool-call guardrails stop repeated identical tool failures without reading task prose.
# 模块用途: 记录同一 run 内工具名+结构化参数的失败次数，连续失败时返回机器阻断结果，避免工具循环空转。

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..tooling import ToolExecutionResult

_STATE_ATTR = "_tool_call_guardrail_failures"
_NO_PROGRESS_STATE_ATTR = "_tool_call_guardrail_no_progress"
_DEFAULT_EXACT_FAILURE_BLOCK_AFTER = 3
_DEFAULT_NO_PROGRESS_BLOCK_AFTER = 2
_BLOCK_CODE = "TOOL_REPEATED_EXACT_FAILURE"
_NO_PROGRESS_BLOCK_CODE = "TOOL_REPEATED_NO_PROGRESS"
_READ_ONLY_TOOL_NAMES = {
    "fetch_url",
    "http_request",
    "list_files",
    "list_tools",
    "read_artifact",
    "read_file",
    "search",
    "search_text",
}
_LOCAL_PROGRESS_TOOL_NAMES = {
    "append_file",
    "api_json_collection",
    "data_to_workbook",
    "file_write_session",
    "markdown_to_pdf",
    "replace_in_file",
    "write_file",
    "write_structured_json",
}


# LLM: ToolCallSignature is the normalized identity for one tool call within a scoped failure ledger.
# 类用途: 封装工具名、参数指纹和作用域，作为重复失败 guard 的稳定键。
@dataclass(frozen=True)
class ToolCallSignature:
    tool_name: str
    args_hash: str
    scope: str

    # LLM: key returns the tuple form used by the persisted failure counter map.
    # 函数用途: 把签名对象转成可哈希键，供重复失败计数表读写。
    def key(self) -> tuple[str, str, str]:
        return (self.scope, self.tool_name, self.args_hash)


# LLM: maybe_block_repeated_tool_failure blocks exact same failing calls after the configured threshold.
# 函数用途: 在同一作用域里相同工具+参数连续失败达到阈值时，返回结构化阻断结果。
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


def maybe_block_repeated_tool_no_progress(agent: object, params: object, payload: dict[str, object]):
    signature = _signature(params, payload)
    if not signature.tool_name or not _is_read_only_tool(signature.tool_name):
        return None
    record = _no_progress_state(agent).get(signature.key())
    if not isinstance(record, dict) or int(record.get("count") or 0) < _no_progress_block_after(params):
        return None
    return ToolExecutionResult(
        signature.tool_name,
        False,
        json.dumps(
            {
                "error": "repeated identical read-only tool call returned unchanged results",
                "guardrail": {
                    "code": _NO_PROGRESS_BLOCK_CODE,
                    "action": "block",
                    "tool_name": signature.tool_name,
                    "count": int(record.get("count") or 0),
                    "args_hash": signature.args_hash,
                    "result_hash": str(record.get("result_hash") or ""),
                    "scope": signature.scope,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        error_code=_NO_PROGRESS_BLOCK_CODE,
    )


# LLM: record_tool_guard_observation updates the exact-failure ledger after each tool result.
# 函数用途: 记录工具调用成功或失败；失败递增，同结果只读成功递增，本地推进成功会清理无进展计数。
def record_tool_guard_observation(agent: object, runtime_params: object, payload: object, result: ToolExecutionResult) -> None:
    if not isinstance(payload, dict):
        return
    _record_no_progress_observation(agent, runtime_params, payload, result)
    signature = _signature(runtime_params, payload)
    if not signature.tool_name or result.error_code == _BLOCK_CODE:
        return
    failures = _failure_state(agent)
    key = signature.key()
    if result.ok:
        failures.pop(key, None)
        return
    failures[key] = failures.get(key, 0) + 1


def _record_no_progress_observation(
    agent: object,
    runtime_params: object,
    payload: dict[str, object],
    result: ToolExecutionResult,
) -> None:
    signature = _signature(runtime_params, payload)
    if not signature.tool_name or result.error_code == _NO_PROGRESS_BLOCK_CODE:
        return
    state = _no_progress_state(agent)
    if result.ok and _is_local_progress_tool(signature.tool_name):
        state.clear()
        return
    if not (result.ok and _is_read_only_tool(signature.tool_name)):
        return
    result_hash = _result_hash(result.output)
    key = signature.key()
    record = state.get(key)
    if isinstance(record, dict) and record.get("result_hash") == result_hash:
        record["count"] = int(record.get("count") or 0) + 1
        return
    state[key] = {"count": 1, "result_hash": result_hash}


# LLM: _failure_state provides the task-local mutable ledger used by the exact-failure guard.
# 函数用途: 获取或初始化 agent 上的失败计数字典，避免重复失败状态散落在别处。
def _failure_state(agent: object) -> dict[tuple[str, str, str], int]:
    state = getattr(agent, _STATE_ATTR, None)
    if not isinstance(state, dict):
        state = {}
        setattr(agent, _STATE_ATTR, state)
    return state


def _no_progress_state(agent: object) -> dict[tuple[str, str, str], dict[str, object]]:
    state = getattr(agent, _NO_PROGRESS_STATE_ATTR, None)
    if not isinstance(state, dict):
        state = {}
        setattr(agent, _NO_PROGRESS_STATE_ATTR, state)
    return state


# LLM: _signature builds the guardrail identity from structured params and tool payload.
# 函数用途: 根据 payload 和运行参数构造 ToolCallSignature，统一重复失败的比较口径。
def _signature(params: object, payload: dict[str, object]) -> ToolCallSignature:
    tool_name = str(payload.get("tool") or "").strip()
    return ToolCallSignature(tool_name=tool_name, args_hash=_args_hash(payload), scope=_scope(params))


# LLM: _args_hash gives tool payloads a stable digest so repeated exact failures are detected even across dict ordering changes.
# 函数用途: 对工具调用参数生成稳定哈希，供重复失败 guard 比较“是不是同一组参数”。
def _args_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result_hash(value: object) -> str:
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# LLM: _scope chooses the narrowest available runtime identifier so failures do not leak across unrelated runs.
# 函数用途: 优先使用 run_id/request_id/task_id 构造 guard 作用域，避免不同任务互相污染失败计数。
def _scope(params: object) -> str:
    for name in ("run_id", "request_id", "task_id"):
        value = str(getattr(params, name, "") or "").strip()
        if value:
            return f"{name}:{value}"
    return "agent"


def _is_read_only_tool(tool_name: str) -> bool:
    return tool_name in _READ_ONLY_TOOL_NAMES


def _is_local_progress_tool(tool_name: str) -> bool:
    return tool_name in _LOCAL_PROGRESS_TOOL_NAMES


# LLM: _block_after reads the configurable exact-failure threshold while keeping a safe default.
# 函数用途: 从任务属性里读取重复失败阻断阈值；缺失或非法时回退默认值。
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


def _no_progress_block_after(params: object) -> int:
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict):
        value: Any = attrs.get("tool_guard_no_progress_block_after")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            return parsed
    return _DEFAULT_NO_PROGRESS_BLOCK_AFTER


__all__ = [
    "maybe_block_repeated_tool_failure",
    "maybe_block_repeated_tool_no_progress",
    "record_tool_guard_observation",
]
