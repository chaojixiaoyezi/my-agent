# LLM: Tool-call guardrails stop repeated identical tool failures without reading task prose.
# 模块用途: 记录同一 run 内工具名+结构化参数的失败次数，连续失败时返回机器阻断结果，避免工具循环空转。

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..tooling import ToolExecutionResult
from .tool_call_guardrail_config import (
    no_progress_action_block_after,
    no_progress_hint,
    repeat_fail_threshold,
    repeat_failure_hint,
)
from .tool_call_guardrail_config import (
    terminal_block_enabled as configured_terminal_block_enabled,
)

_STATE_ATTR = "_tool_call_guardrail_failures"
_NO_PROGRESS_STATE_ATTR = "_tool_call_guardrail_no_progress"
_BLOCK_CODE = "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED"
_NO_PROGRESS_BLOCK_CODE = "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED"
_READ_ONLY_TOOL_NAMES = {
    "fetch_url",
    "http_request",
    "list_files",
    "list_tools",
    "read_artifact",
    "read_file",
    "search",
    "search_text",
    "web_search",
}
_LOCAL_PROGRESS_TOOL_NAMES = {
    "apply_patch",
    "run_command",
    "write_file",
}

_repeat_fail_threshold = repeat_fail_threshold
_terminal_block_enabled = configured_terminal_block_enabled


# LLM: ToolCallSignature is the normalized identity for one tool call within a scoped failure ledger.
# 类用途: 封装工具名、参数指纹和作用域，作为重复失败 guard 的稳定键。
@dataclass(frozen=True)
class ToolCallSignature:
    tool_name: str
    args_hash: str
    scope: str
    failure_class: str = ""

    # LLM: key returns the tuple form used by the persisted failure counter map.
    # 函数用途: 把签名对象转成可哈希键，供重复失败计数表读写。
    def key(self) -> tuple[str, str, str, str]:
        return (self.scope, self.tool_name, self.args_hash, self.failure_class)


# LLM: maybe_block_repeated_tool_failure intercepts the next unchanged failing action at 3N.
# 函数用途: 在同一作用域里相同工具+参数+同类失败达到 3 倍阈值时，只拦截本次重复动作。
def maybe_block_repeated_tool_failure(agent: object, params: object, payload: dict[str, object]):
    signature = _failure_signature_from_payload(agent, params, payload)
    if not signature.tool_name:
        return None
    failures = _failure_state(agent).get(signature.key(), 0)
    threshold = repeat_fail_threshold(params)
    if threshold <= 0 or failures < threshold * 3:
        return None
    terminal_block_enabled = configured_terminal_block_enabled(params)
    return ToolExecutionResult(
        signature.tool_name,
        False,
        json.dumps(
            {
                "error": "同一工具、同一参数、同类失败已经重复过多次；本次相同调用未执行。请换关键词、换参数、换工具或换数据来源；如果外部条件确实阻塞，请说明已尝试来源和真实阻塞点。",
                "guardrail": {
                    "code": _BLOCK_CODE,
                    "action": "block",
                    "tool_name": signature.tool_name,
                    "count": failures,
                    "args_hash": signature.args_hash,
                    "failure_class": signature.failure_class,
                    "scope": signature.scope,
                    "terminal_block_enabled": terminal_block_enabled,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        result_envelope={
            "runtime_gate": {
                "code": _BLOCK_CODE,
                "action": "terminal_block" if terminal_block_enabled else "block_tool_call",
                "terminal_block_enabled": terminal_block_enabled,
                "tool_name": signature.tool_name,
                "count": failures,
                "args_hash": signature.args_hash,
                "failure_class": signature.failure_class,
                "scope": signature.scope,
            }
        },
        error_code=_BLOCK_CODE,
    )


def maybe_block_repeated_tool_no_progress(agent: object, params: object, payload: dict[str, object]):
    signature = _signature(params, payload)
    if not signature.tool_name or not _is_read_only_tool(signature.tool_name):
        return None
    record = _no_progress_state(agent).get(signature.key())
    block_after = no_progress_action_block_after(params)
    if block_after <= 0 or not isinstance(record, dict) or int(record.get("count") or 0) < block_after:
        return None
    terminal_block_enabled = configured_terminal_block_enabled(params)
    return ToolExecutionResult(
        signature.tool_name,
        False,
        json.dumps(
            {
                "error": "同一只读工具、同一参数已经多次返回相同结果；本次相同调用未执行。请使用已有结果、换查询条件、换工具或换数据来源。",
                "guardrail": {
                    "code": _NO_PROGRESS_BLOCK_CODE,
                    "action": "block",
                    "tool_name": signature.tool_name,
                    "count": int(record.get("count") or 0),
                    "args_hash": signature.args_hash,
                    "result_hash": str(record.get("result_hash") or ""),
                    "scope": signature.scope,
                    "terminal_block_enabled": terminal_block_enabled,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        result_envelope={
            "runtime_gate": {
                "code": _NO_PROGRESS_BLOCK_CODE,
                "action": "terminal_block" if terminal_block_enabled else "block_tool_call",
                "terminal_block_enabled": terminal_block_enabled,
                "tool_name": signature.tool_name,
                "count": int(record.get("count") or 0),
                "args_hash": signature.args_hash,
                "result_hash": str(record.get("result_hash") or ""),
                "scope": signature.scope,
            }
        },
        error_code=_NO_PROGRESS_BLOCK_CODE,
    )


# LLM: record_tool_guard_observation updates the exact-failure ledger after each tool result.
# 函数用途: 记录工具调用成功或失败；失败递增，同结果只读成功递增，本地推进成功会清理无进展计数。
def record_tool_guard_observation(agent: object, runtime_params: object, payload: object, result: ToolExecutionResult) -> str:
    if not isinstance(payload, dict):
        return ""
    no_progress_hint = _record_no_progress_observation(agent, runtime_params, payload, result)
    signature = _signature(runtime_params, payload, _failure_class(result))
    if not signature.tool_name or result.error_code == _BLOCK_CODE:
        return no_progress_hint
    failures = _failure_state(agent)
    key = signature.key()
    if result.ok:
        _clear_failure_counts_for_call(failures, signature)
        _set_last_failure_class(agent, signature, "")
        return no_progress_hint
    count = failures.get(key, 0) + 1
    failures[key] = count
    _set_last_failure_class(agent, signature, signature.failure_class)
    return repeat_failure_hint(runtime_params, signature, count) or no_progress_hint


def _record_no_progress_observation(
    agent: object,
    runtime_params: object,
    payload: dict[str, object],
    result: ToolExecutionResult,
) -> str:
    signature = _signature(runtime_params, payload)
    if not signature.tool_name or result.error_code == _NO_PROGRESS_BLOCK_CODE:
        return ""
    state = _no_progress_state(agent)
    if result.ok and _is_local_progress_tool(signature.tool_name):
        state.clear()
        return ""
    if not (result.ok and _is_read_only_tool(signature.tool_name)):
        return ""
    result_hash = _result_hash(result.output)
    key = signature.key()
    record = state.get(key)
    if isinstance(record, dict) and record.get("result_hash") == result_hash:
        count = int(record.get("count") or 0) + 1
        record["count"] = count
        return no_progress_hint(runtime_params, signature, count)
    state[key] = {"count": 1, "result_hash": result_hash}
    return ""


# LLM: _failure_state provides the task-local mutable ledger used by the exact-failure guard.
# 函数用途: 获取或初始化 agent 上的失败计数字典，避免重复失败状态散落在别处。
def _failure_state(agent: object) -> dict[tuple[str, str, str, str], int]:
    state = getattr(agent, _STATE_ATTR, None)
    if not isinstance(state, dict):
        state = {}
        setattr(agent, _STATE_ATTR, state)
    return state


def _no_progress_state(agent: object) -> dict[tuple[str, str, str, str], dict[str, object]]:
    state = getattr(agent, _NO_PROGRESS_STATE_ATTR, None)
    if not isinstance(state, dict):
        state = {}
        setattr(agent, _NO_PROGRESS_STATE_ATTR, state)
    return state


# LLM: _signature builds the guardrail identity from structured params and tool payload.
# 函数用途: 根据 payload 和运行参数构造 ToolCallSignature，统一重复失败的比较口径。
def _signature(params: object, payload: dict[str, object], failure_class: str = "") -> ToolCallSignature:
    tool_name = str(payload.get("tool") or "").strip()
    return ToolCallSignature(
        tool_name=tool_name,
        args_hash=_args_hash(payload),
        scope=_scope(params),
        failure_class=failure_class,
    )


def _failure_signature_from_payload(agent: object, params: object, payload: dict[str, object]) -> ToolCallSignature:
    signature = _signature(params, payload)
    failure_class = _last_failure_class(agent, signature.scope, signature.tool_name, signature.args_hash)
    return ToolCallSignature(
        tool_name=signature.tool_name,
        args_hash=signature.args_hash,
        scope=signature.scope,
        failure_class=failure_class,
    )


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


def _failure_class(result: ToolExecutionResult) -> str:
    code = str(getattr(result, "error_code", "") or "").strip()
    if code:
        return f"code:{code}"
    category = str(getattr(result, "error_category", "") or "").strip()
    if category:
        return f"category:{category}"
    return f"output:{_result_hash(result.output)}"


def _last_failure_class(agent: object, scope: str, tool_name: str, args_hash: str) -> str:
    state = getattr(agent, "_tool_call_guardrail_last_failure_class", None)
    if not isinstance(state, dict):
        return ""
    return str(state.get((scope, tool_name, args_hash)) or "")


def _set_last_failure_class(agent: object, signature: ToolCallSignature, failure_class: str) -> None:
    state = getattr(agent, "_tool_call_guardrail_last_failure_class", None)
    if not isinstance(state, dict):
        state = {}
        agent._tool_call_guardrail_last_failure_class = state
    key = (signature.scope, signature.tool_name, signature.args_hash)
    if failure_class:
        state[key] = failure_class
    else:
        state.pop(key, None)


def _clear_failure_counts_for_call(
    failures: dict[tuple[str, str, str, str], int],
    signature: ToolCallSignature,
) -> None:
    for key in list(failures):
        if key[:3] == (signature.scope, signature.tool_name, signature.args_hash):
            failures.pop(key, None)


__all__ = [
    "maybe_block_repeated_tool_failure",
    "maybe_block_repeated_tool_no_progress",
    "record_tool_guard_observation",
]
