# LLM: 安全工具可见性和执行授权在这里落地，改动前核对认证边界。
# 模块用途: 工具调用解析、授权校验、参数准备和异常格式化。

from __future__ import annotations

"""parsing and execution helpers for ToolRegistry.

给人看的解释：
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
    legacy_payloads_to_tool_envelopes,
    payload_from_tool_call_envelope,
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


# LLM: ExecuteRegistryCallParams 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 工具执行参数包，集中保存调用上下文、授权和安全策略。
@dataclass(frozen=True)
class ExecuteRegistryCallParams:
    payload: object
    tools: dict[str, BaseTool]
    workspace_root: Path
    workspace_roots: list[Path] | None
    expose_security_tools: bool
    security_tool_names: set[str]
    path_access_mode: str = "normal"
    path_dangerous_roots: list[str] | None = None
    allowed_tools: list[str] | None = None
    granted_capabilities: list[str] | None = None
    write_boundary: dict[str, object] | None = None
    payload_limits: ToolPayloadNormalizeLimits | None = None


# LLM: _ToolBlockParseContext bundles parser state to keep helper signatures small.
# 类用途: 保存当前文本工具块解析的原文、输出列表和 payload 预算。
@dataclass
class _ToolBlockParseContext:
    calls: list[tuple[int, dict[str, Any]]]
    scan_text: str
    payload_limits: ToolPayloadNormalizeLimits | None


# LLM: parse_registry_tool_calls 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 解析 parse_registry_tool_calls 数据结构。
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


# LLM: _parse_tool_block_calls extracts JSON tool blocks while preserving source order.
# 函数用途: 解析 `[TOOL_CALL]...[/TOOL_CALL]` 块，并把缺结束标记、嵌套坏块转成结构化 parse error。
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


# LLM: _append_unclosed_tool_block records a missing-end-marker parse error without aborting parsing.
# 函数用途: 处理没有 `[/TOOL_CALL]` 的尾部工具块，并保留可解析 payload 时的兼容结果。
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


# LLM: _append_closed_tool_block records one complete tool block and returns the next cursor.
# 函数用途: 处理完整工具块；嵌套坏块时把坏块转成 parse error 并让外层循环从嵌套处继续。
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


# LLM: parse_registry_tool_call_envelopes is the typed bridge for legacy text tool calls.
# 函数用途: 复用旧文本 parser，但把结果立即包装成 ToolCallEnvelope，避免业务层继续直接消费自然语言块。
def parse_registry_tool_call_envelopes(
    text: str,
    *,
    scope: RunScope | None = None,
    source: str = "legacy_text_protocol",
) -> list[ToolCallEnvelope]:
    return legacy_payloads_to_tool_envelopes(
        parse_registry_tool_calls(text),
        scope=scope,
        source=source,
    )


# LLM: execute_registry_call 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 execute_registry_call 步骤，并保持调用方依赖的数据形状。
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


# LLM: _prepare_tool_payload 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 整理工具调用的 prepare_tool_payload 信息，供注册表鉴权或执行使用。
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


# LLM: _normalized_payload_or_error bundles parse and payload normalization failures.
# 函数用途: 将执行 payload 归一为 dict；解析失败时直接返回带 envelope 的工具错误。
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


# LLM: _with_resolved_dispatch_tool_name rewrites only deterministic tool-name drift.
# 函数用途: 在工具网关执行前修正大小写/命名空间后缀，不执行 fuzzy suggestion。
def _with_resolved_dispatch_tool_name(
    payload: dict[str, Any],
    call: ExecuteRegistryCallParams,
) -> dict[str, Any]:
    resolved = resolve_dispatch_tool_name(payload.get("tool"), call.tools.keys())
    if not resolved or resolved == payload.get("tool"):
        return payload
    return {**payload, "tool": resolved}


# LLM: _invoke_registry_with_envelope invokes the selected tool and preserves call envelope refs.
# 函数用途: 把 RegistryToolInvokeRequest 的构造从主入口拆出，降低执行入口复杂度。
def _invoke_registry_with_envelope(
    call: ExecuteRegistryCallParams,
    envelope: ToolCallEnvelope | None,
    tool_name: str,
    payload: dict[str, Any],
) -> ToolExecutionResult:
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


# LLM: _registry_auth_error 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 registry_auth_error 步骤，并保持调用方依赖的数据形状。
def _registry_auth_error(tool_name: str, call: ExecuteRegistryCallParams) -> str:
    return registry_auth_error(
        tool_name,
        ToolAuthContext(
            allowed=allowed_tool_set(call.allowed_tools),
            granted_capabilities=call.granted_capabilities,
            expose_security_tools=call.expose_security_tools,
            security_tool_names=call.security_tool_names,
        ),
    )
