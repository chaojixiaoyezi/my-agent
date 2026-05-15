# LLM: Final registry invocation step for normalized tool payloads.
# 模块用途: 在工具 payload 已解析和授权后，集中完成参数准备、写边界检查和真实工具执行。

from __future__ import annotations

"""Execute an authorized registry tool after parsing and auth checks."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import BaseTool, ToolExecutionResult
from .registry_params import tool_params_for_execution
from .registry_tool_dispatch import AuthorizedToolDispatchRequest, execute_authorized_tool
from .write_boundary import WRITE_TOOL_NAMES, validate_write_boundary

_BOUNDARY_FILESYSTEM_TOOL_NAMES = WRITE_TOOL_NAMES | {
    "list_files",
    "read_file",
    "search_text",
}


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
    workspace_roots = _workspace_roots_for_invocation(request)
    boundary_error = validate_write_boundary(
        request.tool_name,
        tool_params,
        workspace_root=request.workspace_root,
        workspace_roots=workspace_roots,
        write_boundary=request.write_boundary,
    )
    if boundary_error:
        return ToolExecutionResult(request.tool_name, False, boundary_error)

    return _execute_with_workspace_roots(
        tool,
        workspace_roots,
        lambda: execute_authorized_tool(
            AuthorizedToolDispatchRequest(
                tool_name=request.tool_name,
                tool=tool,
                tool_params=tool_params,
                workspace_root=request.workspace_root,
                write_boundary=request.write_boundary,
            )
        )
    )


# LLM: _workspace_roots_for_invocation lets parent-granted product roots reach filesystem tools.
# 函数用途: 写工具执行时把 write_boundary 明确授权的产物目录临时并入工作区根，避免外部产物目录被工具层误拒。
def _workspace_roots_for_invocation(request: RegistryToolInvokeRequest) -> list[Path] | None:
    roots = _normalized_roots(request.workspace_root, request.workspace_roots)
    if request.tool_name not in _BOUNDARY_FILESYSTEM_TOOL_NAMES or not isinstance(request.write_boundary, dict):
        return roots
    for key in ("allowed_write_roots", "product_write_roots", "task_dir"):
        _append_boundary_roots(roots, request.write_boundary.get(key), request.workspace_root)
    return roots


# LLM: _execute_with_workspace_roots scopes temporary filesystem root expansion to one tool call.
# 函数用途: 只在当前工具执行期间替换 tool.workspace_roots，执行后恢复，避免授权根污染后续无关调用。
def _execute_with_workspace_roots(
    tool: BaseTool,
    workspace_roots: list[Path] | None,
    callback: Callable[[], ToolExecutionResult],
) -> ToolExecutionResult:
    if not workspace_roots or not hasattr(tool, "workspace_roots"):
        return callback()
    old_roots = tool.workspace_roots
    tool.workspace_roots = workspace_roots
    try:
        return callback()
    finally:
        tool.workspace_roots = old_roots


# LLM: _append_boundary_roots normalizes absolute and relative write-boundary roots without validating business policy.
# 函数用途: 从 write_boundary 取路径字符串，转成可供文件工具 resolve_path 使用的根目录列表。
def _append_boundary_roots(roots: list[Path], value: object, workspace_root: Path) -> None:
    values = value if isinstance(value, list) else [value]
    for raw in values:
        if not raw:
            continue
        path = Path(str(raw))
        if not path.is_absolute():
            path = workspace_root / path
        resolved = path.resolve(strict=False)
        if resolved not in roots:
            roots.append(resolved)


# LLM: _normalized_roots mirrors filesystem root normalization for registry-level tool execution.
# 函数用途: 统一 primary root 与额外 workspace_roots，保持顺序去重。
def _normalized_roots(primary: Path, roots: list[Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve(strict=False)
        if path not in resolved:
            resolved.append(path)
    return resolved
