
from __future__ import annotations

"""Execute an authorized registry tool after parsing and auth checks."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .controlled_exec import ControlledExecToolRequest, execute_controlled_exec_tool
from .models import BaseTool, ToolExecutionResult
from .write_boundary import WRITE_TOOL_NAMES, validate_write_boundary

_MAX_EXCEPTION_MESSAGE_CHARS = 500
_BOUNDARY_FILESYSTEM_TOOL_NAMES = WRITE_TOOL_NAMES | {
    "find_files",
    "list_files",
    "read_file",
    "search_text",
}
_TASK_WORKSPACE_RELATIVE_PATH_TOOL_NAMES = _BOUNDARY_FILESYSTEM_TOOL_NAMES


@dataclass(frozen=True)
class RegistryToolInvokeRequest:
    tool_name: str
    payload: dict[str, Any]
    tools: dict[str, BaseTool]
    workspace_root: Path
    workspace_roots: list[Path] | None
    allowed_tools: list[str] | None
    write_boundary: dict[str, object] | None
    path_access_mode: str = "normal"
    path_dangerous_roots: list[str] | None = None


@dataclass(frozen=True)
class AuthorizedToolDispatchRequest:
    tool_name: str
    tool: BaseTool
    tool_params: dict[str, Any]
    workspace_root: Path
    write_boundary: dict[str, object] | None


def invoke_registry_tool(request: RegistryToolInvokeRequest) -> ToolExecutionResult:
    tool = request.tools.get(request.tool_name)
    if tool is None:
        return ToolExecutionResult(request.tool_name, False, f"未知工具: {request.tool_name}")

    tool_params = _tool_params_for_execution(request.payload, request.tool_name, request.allowed_tools)
    tool_params = _with_task_workspace_relative_path(tool_params, request)
    workspace_roots = _workspace_roots_for_invocation(request)
    boundary_error = validate_write_boundary(
        request.tool_name,
        tool_params,
        workspace_root=request.workspace_root,
        workspace_roots=workspace_roots,
        path_access_mode=request.path_access_mode,
        path_dangerous_roots=request.path_dangerous_roots,
        write_boundary=request.write_boundary,
    )
    if boundary_error:
        return ToolExecutionResult(request.tool_name, False, boundary_error)

    return _execute_with_temporary_tool_context(
        tool,
        workspace_roots=workspace_roots,
        allowed_private_hosts=_boundary_string_tuple(request.write_boundary, "allowed_private_hosts"),
        allow_private_resolution=_boundary_bool(request.write_boundary, "allow_private_resolution"),
        callback=lambda: execute_authorized_tool(
            AuthorizedToolDispatchRequest(
                tool_name=request.tool_name,
                tool=tool,
                tool_params=tool_params,
                workspace_root=request.workspace_root,
                write_boundary=request.write_boundary,
            )
        ),
    )


def _tool_params_for_execution(
    normalized_payload: dict[str, Any],
    tool_name: str,
    allowed_tools: list[str] | None,
) -> dict[str, Any]:
    params = {key: value for key, value in normalized_payload.items() if key != "tool"}
    if tool_name == "read_file" and allowed_tools is not None:
        params["__allowed_tools"] = list(allowed_tools)
    return params


def _with_task_workspace_relative_path(
    params: dict[str, Any],
    request: RegistryToolInvokeRequest,
) -> dict[str, Any]:
    if request.tool_name not in _TASK_WORKSPACE_RELATIVE_PATH_TOOL_NAMES or not isinstance(request.write_boundary, dict):
        return params
    raw = params.get("path")
    rewritten = _task_workspace_relative_path(raw, request.write_boundary)
    if not rewritten:
        return params
    return {**params, "path": rewritten}


def _task_workspace_relative_path(raw: object, boundary: dict[str, object]) -> str:
    text = str(raw or "").strip()
    if not text or _is_absolute_or_home_path(text):
        return ""
    normalized = text.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    for prefix, root_key in (("output", "task_output_dir"), ("work", "task_work_dir")):
        if normalized == prefix:
            suffix = ""
        elif normalized.startswith(prefix + "/"):
            suffix = normalized[len(prefix) + 1 :]
        else:
            continue
        root = str(boundary.get(root_key) or "").strip()
        if not root:
            return ""
        try:
            base = Path(root).expanduser().resolve(strict=False)
        except OSError:
            return ""
        return str((base / suffix).resolve(strict=False)) if suffix else str(base)
    return ""


def _is_absolute_or_home_path(text: str) -> bool:
    return text.startswith("/") or text.startswith("~") or bool(
        re.match(r"^[A-Za-z]:[\\/]", text) or re.match(r"^\\\\[^\\/]+[\\/][^\\/]+", text)
    )


# Keep the low-level filesystem tools aligned with the current task workspace roots.
def _workspace_roots_for_invocation(request: RegistryToolInvokeRequest) -> list[Path] | None:
    roots = _normalized_roots(request.workspace_root, request.workspace_roots)
    if request.tool_name not in _BOUNDARY_FILESYSTEM_TOOL_NAMES or not isinstance(request.write_boundary, dict):
        return roots
    for key in (
        "allowed_read_roots",
        "allowed_write_roots",
        "product_write_roots",
        "task_dir",
        "task_root",
        "task_output_dir",
        "task_work_dir",
    ):
        _append_boundary_roots(roots, request.write_boundary.get(key), request.workspace_root)
    return roots


def _execute_with_temporary_tool_context(
    tool: BaseTool,
    *,
    workspace_roots: list[Path] | None,
    allowed_private_hosts: tuple[str, ...],
    allow_private_resolution: bool | None,
    callback: Callable[[], ToolExecutionResult],
) -> ToolExecutionResult:
    old_roots = getattr(tool, "workspace_roots", None)
    old_allowed_private_hosts = getattr(tool, "allowed_private_hosts", None)
    old_allow_private_resolution = getattr(tool, "allow_private_resolution", None)
    if workspace_roots and hasattr(tool, "workspace_roots"):
        tool.workspace_roots = workspace_roots
    if allowed_private_hosts and hasattr(tool, "allowed_private_hosts"):
        tool.allowed_private_hosts = allowed_private_hosts
    if allow_private_resolution is not None and hasattr(tool, "allow_private_resolution"):
        tool.allow_private_resolution = allow_private_resolution
    try:
        return callback()
    finally:
        if workspace_roots and hasattr(tool, "workspace_roots"):
            tool.workspace_roots = old_roots
        if allowed_private_hosts and hasattr(tool, "allowed_private_hosts"):
            tool.allowed_private_hosts = old_allowed_private_hosts
        if allow_private_resolution is not None and hasattr(tool, "allow_private_resolution"):
            tool.allow_private_resolution = old_allow_private_resolution


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


def _boundary_string_tuple(boundary: dict[str, object] | None, key: str) -> tuple[str, ...]:
    if not isinstance(boundary, dict):
        return ()
    value = boundary.get(key)
    items = value if isinstance(value, list) else []
    return tuple(str(item).strip() for item in items if str(item).strip())


def _boundary_bool(boundary: dict[str, object] | None, key: str) -> bool | None:
    if not isinstance(boundary, dict) or key not in boundary:
        return None
    value = boundary.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true"}


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


def _normalized_roots(primary: Path, roots: list[Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve(strict=False)
        if path not in resolved:
            resolved.append(path)
    return resolved
