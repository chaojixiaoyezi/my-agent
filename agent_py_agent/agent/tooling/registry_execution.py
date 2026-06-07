
from __future__ import annotations

"""parsing and execution helpers for ToolRegistry.

ToolRegistry 本身保持'服务台'职责；这里集中放工具调用解析、授权检查和异常格式化，
避免注册表类继续变厚。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..action_protocol import (
    RunScope,
    ToolCallEnvelope,
)
from ..contracts.gates import GateDecision
from .content_transport_policy import (
    RECOMMENDED_WRITE_CHUNK_CHARS,
    RECOVERY_WRITE_CHUNK_CHARS,
)
from .models import BaseTool, ToolExecutionResult
from .parser import parse_xmlish_tool_calls
from .registry_auth import (
    ToolAuthContext,
    allowed_tool_set,
    registry_auth_error,
    registry_auth_error_code,
    security_tools_visible,
)
from .registry_envelopes import (
    attach_result_envelope,
    payload_from_tool_call_envelope,
    payloads_to_tool_envelopes,
    tool_call_envelope_from_execution_payload,
)
from .registry_file_write_blocks import (
    malformed_file_write_raw_block_calls,
    parse_write_file_raw_blocks,
    write_file_raw_block_ranges,
)
from .registry_invoke import RegistryToolInvokeRequest, invoke_registry_tool
from .registry_payload_normalize import (
    ToolPayloadNormalizeLimits,
    normalize_tool_payload,
    parse_error_payload,
    parse_tool_block_payload,
)
from .registry_payload_normalize import (
    tool_name as normalize_tool_name,
)
from .registry_resilience import resilient_tool_invoke
from .registry_runtime_gate_pipeline import tool_call_gate_decision

_PARSE_RETRY_HINT = (
    "请重新输出标准工具调用格式：[TOOL_CALL] 后跟一个 JSON 对象，再用 [/TOOL_CALL] 结束；"
    "不要混用未闭合的 XML 标签，也不要在 JSON 外追加正文。"
)
_PROTECTED_MARKER_PAIRS = (
    ("[SUBAGENT_RESULT]", "[/SUBAGENT_RESULT]"),
    ("[PARENT_PLANNER_RESULT]", "[/PARENT_PLANNER_RESULT]"),
)
_TRUNCATED_PAYLOAD_HINT = (
    "如果上一轮工具参数太长导致截断，请缩短 goal/plan/acceptance_checks，"
    "只保留关键路径、必需文件名和硬约束；长说明交给后续 runner 自己展开。"
)
_TRUNCATED_WRITE_HINT = (
    "如果上一轮是 write_file 且 content 太长，不要重复输出完整 content；"
    "优先用独立成行的 [WRITE_FILE_RAW path=\"...\"]...[/WRITE_FILE_RAW] 原文块或 write_file.data_base64 提交完整产物；"
    "不要把 WRITE_FILE_RAW 当 JSON tool 名；"
    f"正常分块时单次 content 建议 {RECOMMENDED_WRITE_CHUNK_CHARS} 字符。"
    "如果已经连续解析失败，下一轮只能输出 1 个 write_file 工具调用，"
    f"content 降到不超过 {RECOVERY_WRITE_CHUNK_CHARS} 字符，闭合 [/TOOL_CALL] 后再继续下一块。"
)
_MALFORMED_OPENERS = ("[TOOL_CALL",)
_VALID_OPENERS = ("[TOOL_CALL]",)
_START_MARKERS = ("[TOOL_CALL]",)
_END_MARKERS = ("[/TOOL_CALL]",)


@dataclass(frozen=True)
class ExecuteRegistryCallParams:
    payload: object
    tools: dict[str, BaseTool]
    workspace_root: Path
    workspace_roots: list[Path] | None
    expose_security_tools: bool
    security_tool_names: set[str]
    default_hidden_tool_names: set[str] | None = None
    path_access_mode: str = "normal"
    path_dangerous_roots: list[str] | None = None
    allowed_tools: list[str] | None = None
    disabled_tools: list[str] | None = None
    granted_capabilities: list[str] | None = None
    write_boundary: dict[str, object] | None = None
    payload_limits: ToolPayloadNormalizeLimits | None = None
    runtime_guard_policy: object | None = None


@dataclass
class _ToolBlockParseContext:
    calls: list[tuple[int, dict[str, Any]]]
    scan_text: str
    payload_limits: ToolPayloadNormalizeLimits | None


def parse_registry_tool_calls(
    text: str,
    *,
    payload_limits: ToolPayloadNormalizeLimits | None = None,
) -> list[dict[str, Any]]:

    scan_text = mask_protected_control_ranges(text)
    raw_ranges = _top_level_raw_block_ranges(scan_text)
    calls = _top_level_raw_block_calls(scan_text)
    scan_without_raw = _mask_ranges(scan_text, raw_ranges)
    calls.extend(_parse_tool_block_calls(scan_without_raw, payload_limits=payload_limits))
    top_level_text = _mask_ranges(scan_without_raw, _tool_block_ranges(scan_without_raw))
    calls.extend(malformed_tool_marker_calls(top_level_text))
    calls.extend(malformed_file_write_raw_block_calls(top_level_text))
    calls.extend(parse_xmlish_tool_calls(top_level_text))
    calls.sort(key=lambda item: item[0])
    return [payload for _, payload in calls]


def mask_protected_control_ranges(text: str) -> str:
    ranges = _protected_control_ranges(text)
    if not ranges:
        return text
    chars = list(text)
    for start, end in ranges:
        for index in range(start, end):
            chars[index] = " "
    return "".join(chars)


def _protected_control_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for start_marker, end_marker in _PROTECTED_MARKER_PAIRS:
        ranges.extend(_marker_ranges(text, start_marker, end_marker))
    return sorted(ranges)


def _marker_ranges(text: str, start_marker: str, end_marker: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = text.find(start_marker, cursor)
        if start == -1:
            return ranges
        body_start = start + len(start_marker)
        end = text.find(end_marker, body_start)
        if end == -1:
            ranges.append((start, len(text)))
            return ranges
        ranges.append((start, end + len(end_marker)))
        cursor = end + len(end_marker)


def next_tool_block_start(text: str, cursor: int) -> tuple[int, str] | None:
    starts = [
        (pos, marker)
        for marker in _START_MARKERS
        for pos in [_next_protocol_marker_pos(text, marker, cursor)]
        if pos != -1
    ]
    return min(starts, key=lambda item: item[0]) if starts else None


def next_tool_block_end(text: str, start_at: int) -> tuple[int, str] | None:
    ends = [
        (pos, marker)
        for marker in _END_MARKERS
        for pos in [_next_protocol_marker_pos(text, marker, start_at)]
        if pos != -1
    ]
    return min(ends, key=lambda item: item[0]) if ends else None


def _next_protocol_marker_pos(text: str, marker: str, cursor: int) -> int:
    while True:
        pos = text.find(marker, cursor)
        if pos == -1:
            return -1
        if _marker_starts_protocol_line(text, pos):
            return pos
        cursor = pos + len(marker)


def _marker_starts_protocol_line(text: str, pos: int) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    return not text[line_start:pos].strip()


def _top_level_raw_block_ranges(scan_text: str) -> list[tuple[int, int]]:
    tool_ranges = _tool_block_ranges(scan_text)
    return [
        raw_range
        for raw_range in write_file_raw_block_ranges(scan_text)
        if not _position_in_ranges(raw_range[0], tool_ranges)
    ]


def _top_level_raw_block_calls(scan_text: str) -> list[tuple[int, dict[str, Any]]]:
    tool_ranges = _tool_block_ranges(scan_text)
    return [
        item
        for item in parse_write_file_raw_blocks(scan_text)
        if not _position_in_ranges(item[0], tool_ranges)
    ]


def _parse_tool_block_calls(
    scan_text: str,
    *,
    payload_limits: ToolPayloadNormalizeLimits | None,
) -> list[tuple[int, dict[str, Any]]]:
    calls: list[tuple[int, dict[str, Any]]] = []
    context = _ToolBlockParseContext(calls=calls, scan_text=scan_text, payload_limits=payload_limits)
    cursor = 0
    while True:
        start_info = next_tool_block_start(scan_text, cursor)
        if start_info is None:
            break
        start, marker_start = start_info
        body_start = start + len(marker_start)
        end_info = next_tool_block_end(scan_text, body_start)
        if end_info is None:
            _append_unclosed_tool_block(context, start, body_start)
            break
        cursor = _append_closed_tool_block(context, start, body_start, end_info)
    return calls


def _tool_block_ranges(scan_text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start_info = next_tool_block_start(scan_text, cursor)
        if start_info is None:
            return ranges
        start, marker_start = start_info
        body_start = start + len(marker_start)
        end_info = next_tool_block_end(scan_text, body_start)
        if end_info is None:
            ranges.append((start, len(scan_text)))
            return ranges
        end, marker_end = end_info
        cursor = end + len(marker_end)
        ranges.append((start, cursor))


def _mask_ranges(text: str, ranges: list[tuple[int, int]]) -> str:
    if not ranges:
        return text
    chars = list(text)
    for start, end in ranges:
        for index in range(start, end):
            chars[index] = " "
    return "".join(chars)


def _position_in_ranges(pos: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in ranges)


def malformed_tool_marker_calls(text: str) -> list[tuple[int, dict[str, Any]]]:
    calls = [
        (
            pos,
            parse_error_payload(
                "工具调用开始标记格式错误，缺少 ]",
                _malformed_marker_raw(text, pos),
                error_code="TOOL_CALL_MARKER_MALFORMED",
            ),
        )
        for opener in _MALFORMED_OPENERS
        for pos in _malformed_opener_positions(text, opener)
    ]
    return sorted(calls, key=lambda item: item[0])


def _malformed_opener_positions(text: str, opener: str) -> list[int]:
    positions: list[int] = []
    cursor = 0
    while True:
        pos = text.find(opener, cursor)
        if pos == -1:
            return positions
        cursor = pos + len(opener)
        if _is_malformed_protocol_opener(text, pos, opener):
            positions.append(pos)


def _is_malformed_protocol_opener(text: str, pos: int, opener: str) -> bool:
    return (
        not _is_valid_opener(text, pos)
        and _looks_like_line_start_marker(text, pos, opener)
    )


def _is_valid_opener(text: str, pos: int) -> bool:
    return any(text.startswith(opener, pos) for opener in _VALID_OPENERS)


def _looks_like_line_start_marker(text: str, pos: int, opener: str) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    if text[line_start:pos].strip():
        return False
    next_char = text[pos + len(opener) : pos + len(opener) + 1]
    return not next_char or next_char.isspace() or next_char in {"{", ":"}


def _malformed_marker_raw(text: str, pos: int) -> str:
    end_info = next_tool_block_end(text, pos)
    if end_info is None:
        return text[pos:].strip()
    end, marker = end_info
    return text[pos : end + len(marker)].strip()


def _append_unclosed_tool_block(
    context: _ToolBlockParseContext,
    start: int,
    body_start: int,
) -> None:
    raw = context.scan_text[body_start:].strip().strip("`")
    payload = parse_tool_block_payload(raw, limits=context.payload_limits)
    context.calls.append((
        start,
        parse_error_payload(
            "工具调用缺少结束标记 [/TOOL_CALL]",
            raw,
            limits=context.payload_limits,
            error_code="TOOL_CALL_UNCLOSED",
        )
        if payload.get("tool") == "__parse_error__"
        else payload,
    ))


def _append_closed_tool_block(
    context: _ToolBlockParseContext,
    start: int,
    body_start: int,
    end_info: tuple[int, str],
) -> int:
    end, marker_end = end_info
    raw = context.scan_text[body_start:end].strip().strip("`")
    payload = parse_tool_block_payload(raw, limits=context.payload_limits)
    nested_start_info = next_tool_block_start(context.scan_text, body_start)
    if payload.get("tool") == "__parse_error__" and nested_start_info and nested_start_info[0] < end:
        nested_start = nested_start_info[0]
        malformed_raw = context.scan_text[body_start:nested_start].strip().strip("`")
        context.calls.append((
            start,
            parse_error_payload(
                "工具调用缺少结束标记 [/TOOL_CALL]",
                malformed_raw,
                limits=context.payload_limits,
                error_code="TOOL_CALL_UNCLOSED",
            ),
        ))
        return nested_start
    context.calls.append((start, payload))
    return end + len(marker_end)


def parse_registry_tool_call_envelopes(
    text: str,
    *,
    scope: RunScope | None = None,
    source: str = "text_protocol",
) -> list[ToolCallEnvelope]:
    return payloads_to_tool_envelopes(
        parse_registry_tool_calls(text),
        scope=scope,
        source=source,
    )


def execute_registry_call(call: ExecuteRegistryCallParams) -> ToolExecutionResult:

    envelope = tool_call_envelope_from_execution_payload(call.payload)
    if isinstance(envelope, ToolExecutionResult):
        return envelope
    normalized_payload = _normalized_payload_or_error(call, envelope)
    if isinstance(normalized_payload, ToolExecutionResult):
        return normalized_payload
    wrapped_error = _same_name_wrapper_error(normalized_payload, envelope)
    if wrapped_error:
        return wrapped_error
    try:
        tool_name = normalize_tool_name(normalized_payload.get("tool"), limits=call.payload_limits)
    except ValueError as exc:
        return attach_result_envelope(ToolExecutionResult("unknown", False, str(exc)), envelope)
    auth_error = _registry_auth_error(tool_name, call)
    if auth_error:
        code = _registry_auth_error_code(tool_name, call)
        output = auth_error if not code else f"{code}: {auth_error}"
        return attach_result_envelope(ToolExecutionResult(tool_name, False, output, error_code=code), envelope)
    parameter_error = _unknown_parameter_error(normalized_payload, call.tools.get(tool_name), envelope)
    if parameter_error:
        return parameter_error
    gate_decision = tool_call_gate_decision(normalized_payload, call)
    if not gate_decision.allowed:
        return runtime_gate_block_result(normalized_payload, gate_decision, envelope)
    result = _invoke_registry_with_envelope(call, envelope, tool_name, normalized_payload)
    attach_runtime_gate(result, gate_decision)
    return result


def runtime_gate_block_result(
    payload: dict[str, Any],
    decision: GateDecision,
    envelope: ToolCallEnvelope | None,
) -> ToolExecutionResult:
    result = ToolExecutionResult(
        str(decision.evidence.get("tool_name") or payload.get("tool") or "unknown"),
        False,
        _gate_output(decision),
        error_code=decision.finding_codes[0] if decision.finding_codes else "RUNTIME_GATE_DENIED",
    )
    result = attach_result_envelope(result, envelope)
    attach_runtime_gate(result, decision)
    return result


def attach_runtime_gate(result: ToolExecutionResult, decision: GateDecision) -> None:
    envelope = dict(result.result_envelope or {})
    envelope["runtime_gate"] = decision.to_dict()
    result.result_envelope = envelope


def _gate_output(decision: GateDecision) -> str:
    codes = ",".join(decision.finding_codes) or "RUNTIME_GATE_DENIED"
    hint = " 路径超出允许的工作区范围，请使用工作区内或已授权 root 下的路径。" if _has_path_finding(decision) else ""
    model_message = decision.model_message
    if model_message:
        return (
            f"runtime gate denied: gate={decision.gate}; status={decision.status}; "
            f"findings={codes}; {model_message}{hint}"
        )
    return f"runtime gate denied: gate={decision.gate}; status={decision.status}; findings={codes}; 工具未授权或未通过运行时门{hint}"


def _has_path_finding(decision: GateDecision) -> bool:
    return any(code.startswith("PATH_") for code in decision.finding_codes)


def _prepare_tool_payload(
    payload: object,
    *,
    limits: ToolPayloadNormalizeLimits | None,
) -> dict[str, Any] | ToolExecutionResult:
    if isinstance(payload, ToolCallEnvelope):
        payload = payload_from_tool_call_envelope(payload)
    normalized_payload, payload_error = normalize_tool_payload(payload, limits=limits)
    if payload_error:
        return ToolExecutionResult("unknown", False, payload_error)
    assert normalized_payload is not None
    return normalized_payload


def _normalized_payload_or_error(
    call: ExecuteRegistryCallParams,
    envelope: ToolCallEnvelope | None,
) -> dict[str, Any] | ToolExecutionResult:
    prepared = _prepare_tool_payload(envelope or call.payload, limits=call.payload_limits)
    if isinstance(prepared, ToolExecutionResult):
        return attach_result_envelope(prepared, envelope)
    if prepared.get("tool") == "__parse_error__":
        return attach_result_envelope(
            ToolExecutionResult(
                "__parse_error__",
                False,
                parse_error_message(prepared),
                error_code=str(prepared.get("error_code") or "TOOL_CALL_UNCLOSED"),
            ),
            envelope,
        )
    return prepared


def parse_error_message(payload: dict[str, Any]) -> str:
    error = str(payload.get("error") or "工具调用解析失败")
    error_code = str(payload.get("error_code") or "").strip()
    hint = f"{error}。{_PARSE_RETRY_HINT}"
    if error_code in {"TOOL_CALL_UNCLOSED", "TOOL_INLINE_CONTENT_STREAM_ABORTED"}:
        hint = f"{hint}{_TRUNCATED_PAYLOAD_HINT}"
    raw = str(payload.get("raw") or "")
    is_write_payload = '"write_file"' in raw
    if error_code in {"TOOL_CALL_UNCLOSED", "TOOL_INLINE_CONTENT_STREAM_ABORTED"} and is_write_payload and '"content"' in raw:
        hint = f"{hint}{_TRUNCATED_WRITE_HINT}"
    return hint


def _same_name_wrapper_error(
    payload: dict[str, Any],
    envelope: ToolCallEnvelope | None,
) -> ToolExecutionResult | None:
    tool_text = str(payload.get("tool") or "").strip()
    if not tool_text:
        return None
    if not isinstance(payload.get(tool_text), dict):
        return None
    body = {
        "ok": False,
        "error": "tool parameters must be top-level current fields; same-name wrapper objects are not accepted.",
        "invalid_field": tool_text,
        "how_to_fix": f"Move fields from {tool_text} to the top level next to tool.",
    }
    return attach_result_envelope(
        ToolExecutionResult(
            tool_text,
            False,
            json.dumps(body, ensure_ascii=False, indent=2),
            error_code="TOOL_INVALID_ARGUMENTS",
        ),
        envelope,
    )


_PROTOCOL_PARAMETER_KEYS = frozenset({
    "artifact_refs",
    "call_id",
    "idempotency_key",
    "kind",
    "metadata",
    "operation_id",
    "run_id",
    "schema_version",
    "tool",
})


def _unknown_parameter_error(
    payload: dict[str, Any],
    tool: BaseTool | None,
    envelope: ToolCallEnvelope | None,
) -> ToolExecutionResult | None:
    if tool is None:
        return None
    spec = getattr(tool, "spec", None)
    allowed = set(getattr(spec, "parameters", {}) or {})
    allowed.update(str(item) for item in getattr(spec, "internal_parameters", []) or [])
    if not allowed:
        return None
    unknown = sorted(key for key in payload if key not in _PROTOCOL_PARAMETER_KEYS and key not in allowed)
    if not unknown:
        return None
    tool_name = str(payload.get("tool") or getattr(spec, "name", "") or "unknown")
    body = {
        "ok": False,
        "error": "tool parameters must match this tool's declared schema.",
        "invalid_fields": unknown,
        "allowed_fields": sorted(allowed),
        "how_to_fix": "Put each tool parameter at the top level next to tool, using only allowed_fields.",
    }
    return attach_result_envelope(
        ToolExecutionResult(
            tool_name,
            False,
            json.dumps(body, ensure_ascii=False, indent=2),
            error_code="TOOL_INVALID_ARGUMENTS",
        ),
        envelope,
    )


def _invoke_registry_with_envelope(
    call: ExecuteRegistryCallParams,
    envelope: ToolCallEnvelope | None,
    tool_name: str,
    payload: dict[str, Any],
) -> ToolExecutionResult:
    payload = _with_execution_scope(payload, envelope)
    request = RegistryToolInvokeRequest(
        tool_name=tool_name,
        payload=payload,
        tools=call.tools,
        workspace_root=call.workspace_root,
        workspace_roots=call.workspace_roots,
        allowed_tools=call.allowed_tools,
        write_boundary=call.write_boundary,
        path_access_mode=call.path_access_mode,
        path_dangerous_roots=call.path_dangerous_roots,
    )
    return attach_result_envelope(
        resilient_tool_invoke(
            invoke=lambda: invoke_registry_tool(request),
            spec=call.tools[tool_name].spec,
            payload=payload,
            workspace_root=call.workspace_root,
            write_boundary=call.write_boundary,
        ),
        envelope,
    )


def _with_execution_scope(payload: dict[str, Any], envelope: ToolCallEnvelope | None) -> dict[str, Any]:
    if envelope is None or not envelope.scope.run_id:
        return payload
    return {
        **payload,
        "__run_scope": envelope.scope.to_dict(),
        "__tool_call_id": envelope.call_id,
    }


def _registry_auth_error(tool_name: str, call: ExecuteRegistryCallParams) -> str:
    return registry_auth_error(
        tool_name,
        _registry_auth_context(call),
    )


def _registry_auth_error_code(tool_name: str, call: ExecuteRegistryCallParams) -> str:
    return registry_auth_error_code(tool_name, _registry_auth_context(call))


def _registry_auth_context(call: ExecuteRegistryCallParams) -> ToolAuthContext:
    return ToolAuthContext(
        allowed=allowed_tool_set(call.allowed_tools),
        disabled=allowed_tool_set(call.disabled_tools) or set(),
        default_hidden=allowed_tool_set(call.default_hidden_tool_names) or set(),
        granted_capabilities=call.granted_capabilities,
        expose_security_tools=call.expose_security_tools,
        security_tool_names=call.security_tool_names,
    )
