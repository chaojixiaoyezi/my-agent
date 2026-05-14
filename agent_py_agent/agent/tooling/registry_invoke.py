# LLM: Final registry invocation step for normalized tool payloads.
# 模块用途: 在工具 payload 已解析和授权后，集中完成参数准备、写边界检查和真实工具执行。

from __future__ import annotations

"""Execute an authorized registry tool after parsing and auth checks."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import BaseTool, ToolExecutionResult
from .registry_params import tool_params_for_execution
from .registry_tool_dispatch import AuthorizedToolDispatchRequest, execute_authorized_tool
from .write_boundary import validate_write_boundary


# LLM: RegistryToolInvokeRequest keeps final tool invocation bundle-shaped.
# 类用途: 保存执行一个已授权工具所需的工具表、参数、工作区和写边界策略。
@dataclass(frozen=True)
class RegistryToolInvokeRequest:
    tool_name: str
    payload: dict[str, Any]
    tools: dict[str, BaseTool]
    workspace_root: Path
    workspace_roots: list[Path] | None
    allowed_tools: list[str] | None
    write_boundary: dict[str, object] | None


# LLM: invoke_registry_tool is the only place that dispatches a normalized registry payload.
# 函数用途: 根据工具名找工具、准备执行参数、校验写边界，并返回统一 ToolExecutionResult。
def invoke_registry_tool(request: RegistryToolInvokeRequest) -> ToolExecutionResult:
    tool = request.tools.get(request.tool_name)
    if tool is None:
        return ToolExecutionResult(request.tool_name, False, f"未知工具: {request.tool_name}")

    tool_params = tool_params_for_execution(
        request.payload,
        request.tool_name,
        request.allowed_tools,
    )
    boundary_error = validate_write_boundary(
        request.tool_name,
        tool_params,
        workspace_root=request.workspace_root,
        workspace_roots=request.workspace_roots,
        write_boundary=request.write_boundary,
    )
    if boundary_error:
        return ToolExecutionResult(request.tool_name, False, boundary_error)

    return execute_authorized_tool(
        AuthorizedToolDispatchRequest(
            tool_name=request.tool_name,
            tool=tool,
            tool_params=tool_params,
            workspace_root=request.workspace_root,
            write_boundary=request.write_boundary,
        )
    )
