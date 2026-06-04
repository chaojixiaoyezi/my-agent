
from __future__ import annotations

"""parsing and execution helpers for ToolRegistry.

ToolRegistry 本身保持'服务台'职责；这里集中放工具调用解析、授权检查和异常格式化，
避免注册表类继续变厚。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..action_protocol import (
    RunScope,
    ToolCallEnvelope,
)
from ..contracts.tool_name_resolution import resolve_dispatch_tool_name
from .models import BaseTool, ToolExecutionResult
from .parse_error_hint import parse_error_message
from .parser import parse_xmlish_tool_calls
from .registry_auth import (
    ToolAuthContext,
    allowed_tool_set,
    registry_auth_error,
    security_tools_visible,
)
from .registry_control_ranges import mask_protected_control_ranges
from .registry_envelopes import (
    attach_result_envelope,
    payload_from_tool_call_envelope,
    payloads_to_tool_envelopes,
    tool_call_envelope_from_execution_payload,
)
from .registry_file_write_blocks import (
    malformed_file_write_raw_block_calls,
    parse_write_file_raw_blocks,
)
from .registry_invoke import RegistryToolInvokeRequest, invoke_registry_tool
from .registry_malformed_markers import malformed_tool_marker_calls
from .registry_markers import next_tool_block_end, next_tool_block_start
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
from .registry_runtime_gate_results import attach_runtime_gate, runtime_gate_block_result


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
    calls = _parse_tool_block_calls(scan_text, payload_limits=payload_limits)
    calls.extend(malformed_tool_marker_calls(scan_text))
    calls.extend(parse_write_file_raw_blocks(scan_text))
    calls.extend(malformed_file_write_raw_block_calls(scan_text))
    calls.extend(parse_xmlish_tool_calls(scan_text))
    calls.sort(key=lambda item: item[0])
    return [payload for _, payload in calls]


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


def _append_unclosed_tool_block(
    context: _ToolBlockParseContext,
    start: int,
    body_start: int,
) -> None:
    raw = context.scan_text[body_start:].strip().strip("`")
    payload = parse_tool_block_payload(raw, limits=context.payload_limits)
    context.calls.append((
        start,
        parse_error_payload("工具调用缺少结束标记 [/TOOL_CALL]", raw, limits=context.payload_limits)
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
            parse_error_payload("工具调用缺少结束标记 [/TOOL_CALL]", malformed_raw, limits=context.payload_limits),
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
    normalized_payload = _with_resolved_dispatch_tool_name(normalized_payload, call)
    gate_decision = tool_call_gate_decision(normalized_payload, call)
    if not gate_decision.allowed:
        return runtime_gate_block_result(normalized_payload, gate_decision, envelope)
    try:
        tool_name = normalize_tool_name(normalized_payload.get("tool"), limits=call.payload_limits)
    except ValueError as exc:
        return attach_result_envelope(ToolExecutionResult("unknown", False, str(exc)), envelope)
    auth_error = _registry_auth_error(tool_name, call)
    if auth_error:
        return attach_result_envelope(ToolExecutionResult(tool_name, False, auth_error), envelope)
    result = _invoke_registry_with_envelope(call, envelope, tool_name, normalized_payload)
    attach_runtime_gate(result, gate_decision)
    return result


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
            ToolExecutionResult("__parse_error__", False, parse_error_message(prepared)),
            envelope,
        )
    return prepared


def _with_resolved_dispatch_tool_name(
    payload: dict[str, Any],
    call: ExecuteRegistryCallParams,
) -> dict[str, Any]:
    resolved = resolve_dispatch_tool_name(payload.get("tool"), call.tools.keys())
    if not resolved or resolved == payload.get("tool"):
        return payload
    return {**payload, "tool": resolved}


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
        ToolAuthContext(
            allowed=allowed_tool_set(call.allowed_tools),
            disabled=allowed_tool_set(call.disabled_tools) or set(),
            default_hidden=allowed_tool_set(call.default_hidden_tool_names) or set(),
            granted_capabilities=call.granted_capabilities,
            expose_security_tools=call.expose_security_tools,
            security_tool_names=call.security_tool_names,
        ),
    )
