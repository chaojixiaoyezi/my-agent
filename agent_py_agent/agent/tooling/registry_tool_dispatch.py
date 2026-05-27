# LLM: Registry tool dispatch keeps special tool execution out of registry parsing/auth code.
# 模块用途: 在工具已解析、授权和写边界校验后，统一执行普通工具或 registry-aware 特殊工具。

from __future__ import annotations

"""Authorized tool execution helpers for ToolRegistry."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .controlled_exec import ControlledExecToolRequest, execute_controlled_exec_tool
from .models import BaseTool, ToolExecutionResult

_MAX_EXCEPTION_MESSAGE_CHARS = 500


# LLM: AuthorizedToolDispatchRequest keeps post-auth tool dispatch under the bundle interface rule.
# 类用途: 打包已授权工具执行所需的工具名、实例、参数和 registry 注入上下文。
@dataclass(frozen=True)
class AuthorizedToolDispatchRequest:
    tool_name: str
    tool: BaseTool
    tool_params: dict[str, Any]
    workspace_root: Path
    write_boundary: dict[str, object] | None


# LLM: execute_authorized_tool is called after auth/write-boundary checks and may use registry context.
# 函数用途: 执行已通过授权的工具；controlled_exec 会额外读取 write_boundary 中的父级 grant。
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


# LLM: Runtime boundary params are system-injected and override model payload fields.
# 函数用途: 把子代理有效 shell 权限传给 run_command；模型不能通过 payload 给自己提权。
def _tool_params_with_runtime_boundary(request: AuthorizedToolDispatchRequest) -> dict[str, Any]:
    if request.tool_name != "run_command" or not isinstance(request.write_boundary, dict):
        return request.tool_params
    shell_mode = str(request.write_boundary.get("shell_access_mode") or "").strip()
    if not shell_mode:
        return request.tool_params
    params = dict(request.tool_params)
    params["__access_mode"] = shell_mode
    return params


# LLM: _format_tool_exception returns stable, bounded messages without leaking stack traces.
# 函数用途: 将工具异常转成模型可读的短错误，避免长 traceback 进入上下文。
def _format_tool_exception(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        message = _truncate(str(exc), _MAX_EXCEPTION_MESSAGE_CHARS)
        return f"工具执行失败: {message or exc.__class__.__name__}"
    if isinstance(exc, (OSError, UnicodeError)):
        return f"工具执行失败: {exc.__class__.__name__}；请检查路径、权限或文件编码。"
    return f"工具执行失败: {exc.__class__.__name__}；请检查参数后重试。"


# LLM: _truncate bounds exception text before it is shown back to the model.
# 函数用途: 截断过长错误消息，保留稳定中文提示。
def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... 已截断"
