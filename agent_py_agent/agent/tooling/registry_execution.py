# LLM: 安全工具可见性和执行授权在这里落地，改动前核对认证边界。
# 模块用途: 工具调用解析、授权校验、参数准备和异常格式化。

from __future__ import annotations

"""parsing and execution helpers for ToolRegistry.

给人看的解释：
ToolRegistry 本身保持'服务台'职责；这里集中放工具调用解析、授权检查和异常格式化，
避免注册表类继续变厚。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..log_analysis.capabilities import SECURITY_TOOL_NAMES, has_security_tool_capability
from .json_repair import load_tool_block_json
from .models import BaseTool, ToolExecutionResult
from .parse_error_hint import parse_error_message
from .parser import parse_xmlish_tool_calls
from .registry_tool_dispatch import AuthorizedToolDispatchRequest, execute_authorized_tool
from .write_boundary import validate_write_boundary

_MAX_TOOL_PAYLOAD_FIELDS = 64
_MAX_TOOL_FIELD_NAME_CHARS = 128
_MAX_TOOL_NAME_CHARS = 128
_MAX_PARSE_ERROR_RAW_CHARS = 1000
_MODEL_WRAPPER_PARAM_KEYS = {
    "api",
    "filesystem",
    "log_analysis",
    "memory",
    "orchestration",
    "param_name",
    "system",
    "web",
}


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

    calls: list[tuple[int, dict[str, Any]]] = []
    cursor = 0
    while True:
        start_info = _next_tool_block_start(text, cursor)
        if start_info is None:
            break
        start, marker_start = start_info
        end_info = _next_tool_block_end(text, start + len(marker_start))
        if end_info is None:
            raw = text[start + len(marker_start) :].strip().strip("`")
            payload = _parse_tool_block_payload(raw)
            calls.append((start, _parse_error_payload("工具调用缺少结束标记 [/TOOL_CALL]", raw) if payload.get("tool") == "__parse_error__" else payload))
            break
        end, marker_end = end_info
        raw = text[start + len(marker_start) : end].strip().strip("`")
        calls.append((start, _parse_tool_block_payload(raw)))
        cursor = end + len(marker_end)

    calls.extend(parse_xmlish_tool_calls(text))
    calls.sort(key=lambda item: item[0])
    return [payload for _, payload in calls]


# LLM: execute_registry_call 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 execute_registry_call 步骤，并保持调用方依赖的数据形状。
def execute_registry_call(call: ExecuteRegistryCallParams) -> ToolExecutionResult:

    prepared = _prepare_tool_payload(call.payload)
    if isinstance(prepared, ToolExecutionResult):
        return prepared
    normalized_payload = prepared

    if normalized_payload.get("tool") == "__parse_error__":
        return ToolExecutionResult(
            "__parse_error__",
            False,
            parse_error_message(normalized_payload),
        )

    try:
        tool_name = _tool_name(normalized_payload.get("tool"))
    except ValueError as exc:
        return ToolExecutionResult("unknown", False, str(exc))

    auth_error = _registry_auth_error(tool_name, call)
    if auth_error:
        return ToolExecutionResult(tool_name, False, auth_error)

    tool = call.tools.get(tool_name)
    if tool is None:
        return ToolExecutionResult(tool_name, False, f"未知工具: {tool_name}")

    tool_params = {key: value for key, value in normalized_payload.items() if key != "tool"}
    boundary_error = validate_write_boundary(
        tool_name,
        tool_params,
        workspace_root=call.workspace_root,
        workspace_roots=call.workspace_roots,
        write_boundary=call.write_boundary,
    )
    if boundary_error:
        return ToolExecutionResult(tool_name, False, boundary_error)

    return execute_authorized_tool(
        AuthorizedToolDispatchRequest(
            tool_name=tool_name,
            tool=tool,
            tool_params=tool_params,
            workspace_root=call.workspace_root,
            write_boundary=call.write_boundary,
        )
    )


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


# LLM: _prepare_tool_payload 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 整理工具调用的 prepare_tool_payload 信息，供注册表鉴权或执行使用。
def _prepare_tool_payload(payload: object) -> dict[str, Any] | ToolExecutionResult:
    normalized_payload, payload_error = _normalize_tool_payload(payload)
    if payload_error:
        return ToolExecutionResult("unknown", False, payload_error)
    assert normalized_payload is not None
    return normalized_payload


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


# LLM: _next_tool_block_start 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 整理工具调用的 next_tool_block_start 信息，供注册表鉴权或执行使用。
def _next_tool_block_start(text: str, cursor: int) -> tuple[int, str] | None:
    start_markers = ["[TOOL_CALL]", "[SUBAGENT_CALL]"]
    starts = [
        (pos, marker)
        for marker in start_markers
        for pos in [text.find(marker, cursor)]
        if pos != -1
    ]
    return min(starts, key=lambda item: item[0]) if starts else None


# LLM: _next_tool_block_end 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 整理工具调用的 next_tool_block_end 信息，供注册表鉴权或执行使用。
def _next_tool_block_end(text: str, start_at: int) -> tuple[int, str] | None:
    end_markers = ["[/TOOL_CALL]", "[/SUBAGENT_CALL]"]
    ends = [
        (pos, marker)
        for marker in end_markers
        for pos in [text.find(marker, start_at)]
        if pos != -1
    ]
    return min(ends, key=lambda item: item[0]) if ends else None


# LLM: _parse_tool_block_payload 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 解析 parse_tool_block_payload 数据结构。
def _parse_tool_block_payload(raw: str) -> dict[str, Any]:
    try:
        payload = load_tool_block_json(raw)
    except json.JSONDecodeError as exc:
        return _parse_error_payload(f"工具调用 JSON 解析失败: {exc}", raw)
    if not isinstance(payload, dict):
        return _parse_error_payload("工具调用必须是 JSON 对象", raw)
    normalized, error = _normalize_tool_payload(payload)
    if error or normalized is None:
        return _parse_error_payload(error or "工具调用解析失败", raw)
    return normalized


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


# LLM: _parse_error_payload 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 解析 parse_error_payload 数据结构。
def _parse_error_payload(error: str, raw: str) -> dict[str, str]:
    return {
        "tool": "__parse_error__",
        "error": error,
        "raw": _truncate(raw, _MAX_PARSE_ERROR_RAW_CHARS),
    }


# LLM: _normalize_tool_payload 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把输入值归一成 工具系统 内部使用的稳定格式。
def _normalize_tool_payload(payload: object) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(payload, dict):
        return None, "工具调用必须是 JSON 对象"
    normalized, error = _normalize_payload_mapping(payload)
    if error:
        return None, error
    expanded, error = _unwrap_param_name_bundle(normalized)
    if error:
        return None, error
    if len(expanded) > _MAX_TOOL_PAYLOAD_FIELDS:
        return None, f"工具调用字段过多，最多 {_MAX_TOOL_PAYLOAD_FIELDS} 个字段"
    return expanded, ""


# LLM: _normalize_payload_mapping validates a tool payload map before dispatch.
# 函数用途: 检查工具参数名是否安全，并把参数键统一转成字符串，避免坏键污染执行层。
def _normalize_payload_mapping(payload: dict[Any, Any]) -> tuple[dict[str, Any], str]:
    if len(payload) > _MAX_TOOL_PAYLOAD_FIELDS:
        return {}, f"工具调用字段过多，最多 {_MAX_TOOL_PAYLOAD_FIELDS} 个字段"

    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        key_text = str(key)
        if not key_text:
            return {}, "工具调用包含空参数名"
        if len(key_text) > _MAX_TOOL_FIELD_NAME_CHARS:
            return {}, f"工具调用参数名过长，最多 {_MAX_TOOL_FIELD_NAME_CHARS} 个字符"
        if any(ord(char) < 32 for char in key_text):
            return {}, "工具调用参数名包含不支持的控制字符"
        normalized[key_text] = value
    return normalized, ""


# LLM: _unwrap_param_name_bundle repairs a common model mistake without hiding real collisions.
# 函数用途: 当模型把真实参数误包进 param_name 字段时，将其展开成工具可执行的扁平参数。
def _unwrap_param_name_bundle(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    wrapper_keys = [key for key in payload if key != "tool"]
    if (
        len(wrapper_keys) != 1
        or wrapper_keys[0] not in _MODEL_WRAPPER_PARAM_KEYS
        or not isinstance(payload.get(wrapper_keys[0]), dict)
    ):
        return payload, ""
    bundled, error = _normalize_payload_mapping(payload[wrapper_keys[0]])
    if error:
        return {}, error
    if "tool" in bundled:
        return {}, f"{wrapper_keys[0]} 参数包不能包含 tool 字段"
    return {"tool": payload["tool"], **bundled}, ""


# LLM: _tool_name 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 整理工具调用的 tool_name 信息，供注册表鉴权或执行使用。
def _tool_name(value: object) -> str:
    if value is None:
        raise ValueError("工具调用缺少 tool 字段")
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError("tool 字段必须是字符串工具名")
    name = str(value).strip()
    if not name:
        raise ValueError("工具调用缺少 tool 字段")
    if len(name) > _MAX_TOOL_NAME_CHARS:
        raise ValueError(f"tool 字段过长，最多 {_MAX_TOOL_NAME_CHARS} 个字符")
    if any(ord(char) < 32 for char in name):
        raise ValueError("tool 字段包含不支持的控制字符")
    return name


# LLM: _truncate 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 truncate 步骤，并保持调用方依赖的数据形状。
def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... 已截断"
