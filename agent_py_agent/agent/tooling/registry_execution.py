from __future__ import annotations

"""LLM: parsing and execution helpers for ToolRegistry.

给人看的解释：
ToolRegistry 本身保持'服务台'职责；这里集中放工具调用解析、授权检查和异常格式化，
避免注册表类继续变厚。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..log_analysis.capabilities import SECURITY_TOOL_NAMES, has_security_tool_capability
from .models import BaseTool, ToolExecutionResult
from .parser import parse_xmlish_tool_calls
from .write_boundary import validate_write_boundary

_MAX_TOOL_PAYLOAD_FIELDS = 64
_MAX_TOOL_FIELD_NAME_CHARS = 128
_MAX_TOOL_NAME_CHARS = 128
_MAX_PARSE_ERROR_RAW_CHARS = 1000
_MAX_EXCEPTION_MESSAGE_CHARS = 500


@dataclass(frozen=True)
class ExecuteRegistryCallParams:
    payload: object
    tools: dict[str, BaseTool]
    workspace_root: Path
    expose_security_tools: bool
    security_tool_names: set[str]
    allowed_tools: list[str] | None = None
    granted_capabilities: list[str] | None = None
    write_boundary: dict[str, object] | None = None


def parse_registry_tool_calls(text: str) -> list[dict[str, Any]]:
    """从模型输出里提取工具调用块。"""

    calls: list[tuple[int, dict[str, Any]]] = []
    cursor = 0
    while True:
        start_info = _next_tool_block_start(text, cursor)
        if start_info is None:
            break
        start, marker_start = start_info
        end_info = _next_tool_block_end(text, start + len(marker_start))
        if end_info is None:
            break
        end, marker_end = end_info
        raw = text[start + len(marker_start) : end].strip().strip("`")
        calls.append((start, _parse_tool_block_payload(raw)))
        cursor = end + len(marker_end)

    calls.extend(parse_xmlish_tool_calls(text))
    calls.sort(key=lambda item: item[0])
    return [payload for _, payload in calls]


def execute_registry_call(call: ExecuteRegistryCallParams) -> ToolExecutionResult:
    """执行单个工具调用。"""

    normalized_payload, payload_error = _normalize_tool_payload(call.payload)
    if payload_error:
        return ToolExecutionResult("unknown", False, payload_error)
    assert normalized_payload is not None

    if normalized_payload.get("tool") == "__parse_error__":
        return ToolExecutionResult(
            "__parse_error__",
            False,
            str(normalized_payload.get("error") or "工具调用解析失败"),
        )

    try:
        tool_name = _tool_name(normalized_payload.get("tool"))
    except ValueError as exc:
        return ToolExecutionResult("unknown", False, str(exc))

    allowed = allowed_tool_set(call.allowed_tools)
    auth_error = _tool_auth_error(
        tool_name,
        allowed=allowed,
        granted_capabilities=call.granted_capabilities,
        expose_security_tools=call.expose_security_tools,
        security_tool_names=call.security_tool_names,
    )
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
        write_boundary=call.write_boundary,
    )
    if boundary_error:
        return ToolExecutionResult(tool_name, False, boundary_error)

    try:
        return tool.execute(tool_params)
    except Exception as exc:
        return ToolExecutionResult(tool_name, False, _format_tool_exception(exc))


def allowed_tool_set(allowed_tools: list[str] | None) -> set[str] | None:
    """把工具 allowlist 规范成集合；None 表示不限制。"""

    if allowed_tools is None:
        return None
    return {str(item) for item in allowed_tools if str(item).strip()}


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


def _next_tool_block_start(text: str, cursor: int) -> tuple[int, str] | None:
    start_markers = ["[TOOL_CALL]", "[SUBAGENT_CALL]"]
    starts = [
        (pos, marker)
        for marker in start_markers
        for pos in [text.find(marker, cursor)]
        if pos != -1
    ]
    return min(starts, key=lambda item: item[0]) if starts else None


def _next_tool_block_end(text: str, start_at: int) -> tuple[int, str] | None:
    end_markers = ["[/TOOL_CALL]", "[/SUBAGENT_CALL]"]
    ends = [
        (pos, marker)
        for marker in end_markers
        for pos in [text.find(marker, start_at)]
        if pos != -1
    ]
    return min(ends, key=lambda item: item[0]) if ends else None


def _parse_tool_block_payload(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _parse_error_payload(f"工具调用 JSON 解析失败: {exc}", raw)
    if not isinstance(payload, dict):
        return _parse_error_payload("工具调用必须是 JSON 对象", raw)
    return payload


def _tool_auth_error(
    tool_name: str,
    *,
    allowed: set[str] | None,
    granted_capabilities: list[str] | None,
    expose_security_tools: bool,
    security_tool_names: set[str],
) -> str:
    if allowed is not None and tool_name not in allowed:
        return f"工具未授权: {tool_name}"
    if tool_name not in security_tool_names:
        return ""
    if _security_tool_call_authorized(
        tool_name,
        expose_security_tools,
        allowed=allowed,
        granted_capabilities=granted_capabilities,
    ):
        return ""
    return f"tool not authorized: {tool_name}"


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


def _parse_error_payload(error: str, raw: str) -> dict[str, str]:
    return {
        "tool": "__parse_error__",
        "error": error,
        "raw": _truncate(raw, _MAX_PARSE_ERROR_RAW_CHARS),
    }


def _normalize_tool_payload(payload: object) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(payload, dict):
        return None, "工具调用必须是 JSON 对象"
    if len(payload) > _MAX_TOOL_PAYLOAD_FIELDS:
        return None, f"工具调用字段过多，最多 {_MAX_TOOL_PAYLOAD_FIELDS} 个字段"

    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        key_text = str(key)
        if not key_text:
            return None, "工具调用包含空参数名"
        if len(key_text) > _MAX_TOOL_FIELD_NAME_CHARS:
            return None, f"工具调用参数名过长，最多 {_MAX_TOOL_FIELD_NAME_CHARS} 个字符"
        if any(ord(char) < 32 for char in key_text):
            return None, "工具调用参数名包含不支持的控制字符"
        normalized[key_text] = value
    return normalized, ""


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


def _format_tool_exception(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        message = _truncate(str(exc), _MAX_EXCEPTION_MESSAGE_CHARS)
        return f"工具执行失败: {message or exc.__class__.__name__}"
    if isinstance(exc, (OSError, UnicodeError)):
        return f"工具执行失败: {exc.__class__.__name__}；请检查路径、权限或文件编码。"
    return f"工具执行失败: {exc.__class__.__name__}；请检查参数后重试。"


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... 已截断"
