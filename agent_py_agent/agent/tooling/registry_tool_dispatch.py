
from __future__ import annotations

"""Authorized tool execution helpers for ToolRegistry."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .controlled_exec import ControlledExecToolRequest, execute_controlled_exec_tool
from .models import BaseTool, ToolExecutionResult

_MAX_EXCEPTION_MESSAGE_CHARS = 500


@dataclass(frozen=True)
class AuthorizedToolDispatchRequest:
    tool_name: str
    tool: BaseTool
    tool_params: dict[str, Any]
    workspace_root: Path
    write_boundary: dict[str, object] | None


def execute_authorized_tool(request: AuthorizedToolDispatchRequest) -> ToolExecutionResult:
    if request.tool_name == "controlled_exec":
        return execute_controlled_exec_tool(
            ControlledExecToolRequest(
                params=request.tool_params,
                workspace_root=request.workspace_root,
                write_boundary=request.write_boundary,
            )
        )
    try:
        return request.tool.execute(_tool_params_with_runtime_boundary(request))
    except Exception as exc:
        return ToolExecutionResult(request.tool_name, False, _format_tool_exception(exc))


def _tool_params_with_runtime_boundary(request: AuthorizedToolDispatchRequest) -> dict[str, Any]:
    if request.tool_name != "run_command" or not isinstance(request.write_boundary, dict):
        return request.tool_params
    shell_mode = str(request.write_boundary.get("shell_access_mode") or "").strip()
    if not shell_mode:
        return request.tool_params
    params = dict(request.tool_params)
    params["__access_mode"] = shell_mode
    return params


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
