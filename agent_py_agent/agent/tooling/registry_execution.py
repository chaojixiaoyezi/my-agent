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
from ..log_analysis.capabilities import SECURITY_TOOL_NAMES, has_security_tool_capability
from .models import BaseTool, ToolExecutionResult
from .parse_error_hint import parse_error_message
from .parser import parse_xmlish_tool_calls
from .registry_control_ranges import mask_protected_control_ranges
from .registry_envelopes import (
    attach_result_envelope,
    legacy_payloads_to_tool_envelopes,
    payload_from_tool_call_envelope,
    tool_call_envelope_from_execution_payload,
)
from .registry_file_write_blocks import (
    malformed_file_write_raw_block_calls,
    parse_file_write_session_raw_blocks,
    parse_write_file_raw_blocks,
)
from .registry_invoke import RegistryToolInvokeRequest, invoke_registry_tool
from .registry_malformed_markers import malformed_tool_marker_calls
from .registry_markers import next_tool_block_end, next_tool_block_start
from .registry_payload_normalize import (
    normalize_tool_payload,
    parse_error_payload,
    parse_tool_block_payload,
)
from .registry_payload_normalize import (
    tool_name as normalize_tool_name,
)


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
    allowed_tools: list[str] | None = None
    granted_capabilities: list[str] | None = None
    write_boundary: dict[str, object] | None = None


# LLM: _ToolAuthContext 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 工具授权上下文，保存安全工具可见性和允许调用集合。
@dataclass(frozen=True)
class _ToolAuthContext:
    allowed: set[str] | None
    granted_capabilities: list[str] | None
    expose_security_tools: bool
    security_tool_names: set[str]


# LLM: parse_registry_tool_calls 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 解析 parse_registry_tool_calls 数据结构。
def parse_registry_tool_calls(text: str) -> list[dict[str, Any]]:

    scan_text = mask_protected_control_ranges(text)
    calls: list[tuple[int, dict[str, Any]]] = []
    cursor = 0
    while True:
        start_info = next_tool_block_start(scan_text, cursor)
        if start_info is None:
            break
        start, marker_start = start_info
        end_info = next_tool_block_end(scan_text, start + len(marker_start))
        if end_info is None:
            raw = scan_text[start + len(marker_start) :].strip().strip("`")
            payload = parse_tool_block_payload(raw)
            calls.append(
                (
                    start,
                    parse_error_payload("工具调用缺少结束标记 [/TOOL_CALL]", raw)
                    if payload.get("tool") == "__parse_error__"
                    else payload,
                )
            )
            break
        end, marker_end = end_info
        raw = scan_text[start + len(marker_start) : end].strip().strip("`")
        calls.append((start, parse_tool_block_payload(raw)))
        cursor = end + len(marker_end)

    calls.extend(malformed_tool_marker_calls(scan_text))
    calls.extend(parse_write_file_raw_blocks(scan_text))
    calls.extend(parse_file_write_session_raw_blocks(scan_text))
    calls.extend(malformed_file_write_raw_block_calls(scan_text))
    calls.extend(parse_xmlish_tool_calls(scan_text))
    calls.sort(key=lambda item: item[0])
    return [payload for _, payload in calls]


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
    prepared = _prepare_tool_payload(envelope or call.payload)
    if isinstance(prepared, ToolExecutionResult):
        return attach_result_envelope(prepared, envelope)
    normalized_payload = prepared

    if normalized_payload.get("tool") == "__parse_error__":
        return attach_result_envelope(
            ToolExecutionResult(
                "__parse_error__",
                False,
                parse_error_message(normalized_payload),
            ),
            envelope,
        )

    try:
        tool_name = normalize_tool_name(normalized_payload.get("tool"))
    except ValueError as exc:
        return attach_result_envelope(ToolExecutionResult("unknown", False, str(exc)), envelope)

    auth_error = _registry_auth_error(tool_name, call)
    if auth_error:
        return attach_result_envelope(ToolExecutionResult(tool_name, False, auth_error), envelope)

    return attach_result_envelope(
        invoke_registry_tool(
            RegistryToolInvokeRequest(
                tool_name=tool_name,
                payload=normalized_payload,
                tools=call.tools,
                workspace_root=call.workspace_root,
                workspace_roots=call.workspace_roots,
                allowed_tools=call.allowed_tools,
                write_boundary=call.write_boundary,
            )
        ),
        envelope,
    )


# LLM: _prepare_tool_payload 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 整理工具调用的 prepare_tool_payload 信息，供注册表鉴权或执行使用。
def _prepare_tool_payload(payload: object) -> dict[str, Any] | ToolExecutionResult:
    if isinstance(payload, ToolCallEnvelope):
        payload = payload_from_tool_call_envelope(payload)
    normalized_payload, payload_error = normalize_tool_payload(payload)
    if payload_error:
        return ToolExecutionResult("unknown", False, payload_error)
    assert normalized_payload is not None
    return normalized_payload


# LLM: _registry_auth_error 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 registry_auth_error 步骤，并保持调用方依赖的数据形状。
def _registry_auth_error(tool_name: str, call: ExecuteRegistryCallParams) -> str:
    return _tool_auth_error(
        tool_name,
        _ToolAuthContext(
            allowed=allowed_tool_set(call.allowed_tools),
            granted_capabilities=call.granted_capabilities,
            expose_security_tools=call.expose_security_tools,
            security_tool_names=call.security_tool_names,
        ),
    )


# LLM: allowed_tool_set 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 整理工具调用的 allowed_tool_set 信息，供注册表鉴权或执行使用。
def allowed_tool_set(allowed_tools: list[str] | None) -> set[str] | None:

    if allowed_tools is None:
        return None
    return {str(item) for item in allowed_tools if str(item).strip()}


# LLM: security_tools_visible 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 整理工具调用的 security_tools_visible 信息，供注册表鉴权或执行使用。
def security_tools_visible(
    expose_security_tools: bool,
    *,
    allowed: set[str] | None,
    granted_capabilities: list[str] | None,
) -> bool:
    return bool(
        expose_security_tools
        or (allowed is not None and bool(allowed.intersection(SECURITY_TOOL_NAMES)))
        or has_security_tool_capability(granted_capabilities)
    )

# LLM: _tool_auth_error 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 整理工具调用的 tool_auth_error 信息，供注册表鉴权或执行使用。
def _tool_auth_error(
    tool_name: str,
    context: _ToolAuthContext,
) -> str:
    if context.allowed is not None and tool_name not in context.allowed:
        return f"工具未授权: {tool_name}"
    if tool_name not in context.security_tool_names:
        return ""
    if _security_tool_call_authorized(
        tool_name,
        context.expose_security_tools,
        allowed=context.allowed,
        granted_capabilities=context.granted_capabilities,
    ):
        return ""
    return f"tool not authorized: {tool_name}"


# LLM: _security_tool_call_authorized 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 整理工具调用的 security_tool_call_authorized 信息，供注册表鉴权或执行使用。
def _security_tool_call_authorized(
    tool_name: str,
    expose_security_tools: bool,
    *,
    allowed: set[str] | None,
    granted_capabilities: list[str] | None,
) -> bool:
    return bool(
        expose_security_tools
        or (allowed is not None and tool_name in allowed)
        or has_security_tool_capability(granted_capabilities)
    )
