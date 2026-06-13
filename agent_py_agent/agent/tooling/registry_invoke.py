
from __future__ import annotations

"""Execute an authorized registry tool after parsing and auth checks."""

import json
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


@dataclass(frozen=True)
class BoundaryPathCopyRequest:
    params: dict[str, Any]
    boundary: dict[str, object]
    target_key: str
    source_key: str


# 函数用途: 组一条结构化工具错误(批3 长期助手 范式):error 说出了什么错,
#   hint 给可操作下一步,JSON 形态便于模型解析,不再裸文本。
def structured_tool_error(tool_name: str, error: str, hint: str, *, error_code: str) -> ToolExecutionResult:
    payload = json.dumps({"error": error, "hint": hint}, ensure_ascii=False)
    return ToolExecutionResult(tool_name, False, payload, error_code=error_code)


# 函数用途: 写边界校验(越界返回拒绝结果,合规返回 None)。
def _write_boundary_denied(
    request: RegistryToolInvokeRequest, tool_params: dict[str, Any], workspace_roots: tuple[Path, ...]
) -> ToolExecutionResult | None:
    boundary_error = validate_write_boundary(
        request.tool_name,
        tool_params,
        workspace_root=request.workspace_root,
        workspace_roots=workspace_roots,
        path_access_mode=request.path_access_mode,
        path_dangerous_roots=request.path_dangerous_roots,
        write_boundary=request.write_boundary,
    )
    if not boundary_error:
        return None
    return ToolExecutionResult(request.tool_name, False, boundary_error, error_code="WRITE_FORBIDDEN")


def invoke_registry_tool(request: RegistryToolInvokeRequest) -> ToolExecutionResult:
    tool = request.tools.get(request.tool_name)
    if tool is None:
        return structured_tool_error(
            request.tool_name,
            f"未知工具: {request.tool_name}",
            "查看本轮工具目录,使用其中列出的工具名;不要凭记忆猜测工具名。",
            error_code="TOOL_UNAVAILABLE",
        )

    tool_params = _tool_params_for_execution(request.payload, request.tool_name, request.allowed_tools)
    tool_params = _with_task_workspace_relative_path(tool_params, request)
    tool_params = _with_task_artifact_append_continuation(tool_params, request)
    partial_error = _partial_unclosed_write_error(tool_params, request)
    if partial_error:
        return ToolExecutionResult(request.tool_name, False, partial_error, error_code="TOOL_INVALID_ARGUMENTS")
    tool_params = _without_internal_partial_write_marker(tool_params)
    not_ready = _active_child_output_not_ready_result(request, tool_params)
    if not_ready is not None:
        return not_ready
    workspace_roots = _workspace_roots_for_invocation(request)
    boundary_denied = _write_boundary_denied(request, tool_params, workspace_roots)
    if boundary_denied is not None:
        return boundary_denied

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
        if rewritten := _task_workspace_prefixed_path(normalized, prefix, boundary.get(root_key)):
            return rewritten
    return ""


def _task_workspace_prefixed_path(normalized: str, prefix: str, raw_root: object) -> str:
    suffix = _task_workspace_path_suffix(normalized, prefix)
    if suffix is None:
        return ""
    root = str(raw_root or "").strip()
    if not root:
        return ""
    try:
        base = Path(root).expanduser().resolve(strict=False)
    except OSError:
        return ""
    return str((base / suffix).resolve(strict=False)) if suffix else str(base)


def _task_workspace_path_suffix(normalized: str, prefix: str) -> str | None:
    if normalized == prefix:
        return ""
    if normalized.startswith(prefix + "/"):
        return normalized[len(prefix) + 1 :]
    return None


def _with_task_artifact_append_continuation(
    params: dict[str, Any],
    request: RegistryToolInvokeRequest,
) -> dict[str, Any]:
    if request.tool_name != "write_file" or not isinstance(request.write_boundary, dict):
        return params
    if "mode" in params:
        return params
    if "content" not in params or params.get("content") is None:
        return params
    target = _resolved_invocation_path(params.get("path"), request.workspace_root)
    artifact_roots = [
        root
        for root in (
            _resolved_boundary_path(request.write_boundary.get("task_output_dir"), request.workspace_root),
            _resolved_boundary_path(request.write_boundary.get("task_work_dir"), request.workspace_root),
        )
        if root is not None
    ]
    if target is None or not any(_is_relative_to(target, root) for root in artifact_roots):
        return params
    output_json = _resolved_boundary_path(request.write_boundary.get("output_json"), request.workspace_root)
    if output_json is not None and _is_same_path(target, output_json):
        return params
    if not target.exists() or not target.is_file():
        return params
    if _target_is_materialize_placeholder(target):
        return params
    return {**params, "mode": "append", "__implicit_task_artifact_append": True}


def _target_is_materialize_placeholder(target: Path) -> bool:
    """materialize 兜底写的占位由 _render_declared_output_markdown 渲染,固定以
    "# Subagent Result" 开头(gc-test 实锤:子代理声明产物→materialize 先写结果块
    占位→子代理写真报告时 target 已存在→implicit append 把正文追加到占位结果块后,
    结果块元数据混入交付正文)。占位应被子代理真产物覆盖而非追加,故 implicit append
    跳过它(回退覆盖写)。子代理真报告以 "# <主题>" 开头,不会误判。"""
    try:
        with target.open("r", encoding="utf-8") as handle:
            head = handle.read(64)
    except (OSError, UnicodeError):
        return False
    return head.lstrip().startswith("# Subagent Result")


def _partial_unclosed_write_error(params: dict[str, Any], request: RegistryToolInvokeRequest) -> str:
    if request.tool_name != "write_file" or params.get("__partial_unclosed_write") is not True:
        return ""
    target = _resolved_invocation_path(params.get("path"), request.workspace_root)
    if target is None:
        return "未闭合 write_file.content 缺少可解析路径；请重新输出完整工具调用。"
    return "未闭合 write_file.content 不能写入任何目标文件；请输出完整闭合的工具调用，或用 mode=append 分块续写完整闭合的小块。"


def _without_internal_partial_write_marker(params: dict[str, Any]) -> dict[str, Any]:
    if "__partial_unclosed_write" not in params:
        return params
    return {key: value for key, value in params.items() if key != "__partial_unclosed_write"}


def _active_child_output_not_ready_result(
    request: RegistryToolInvokeRequest,
    params: dict[str, Any],
) -> ToolExecutionResult | None:
    if request.tool_name != "read_file" or not isinstance(request.write_boundary, dict):
        return None
    target = _resolved_invocation_path(params.get("path"), request.workspace_root)
    if target is None or target.exists():
        return None
    if not _path_matches_boundary_refs(target, request.write_boundary.get("locked_files"), request.workspace_root):
        return None
    payload = {
        "ok": False,
        "error": "active_child_output_not_ready",
        "message": "Requested path is a declared output for a currently active child agent and is not ready yet.",
        "path": str(target),
        "suggested_tool_call": {"tool": "wait", "seconds": 60, "reason": "wait for active child output"},
        "status_tool_call": {"tool": "inspect_agent_tree", "params": {}},
        "result_fields_to_read": ["child_result_index.read_order", "child_result_index.primary_artifact_refs"],
    }
    return ToolExecutionResult(
        request.tool_name,
        False,
        json.dumps(payload, ensure_ascii=False, indent=2),
        error_code="PATH_NOT_FOUND",
        result_envelope=payload,
    )


def _path_matches_boundary_refs(target: Path, refs: object, workspace_root: Path) -> bool:
    values = refs if isinstance(refs, list) else []
    for ref in values:
        resolved = _resolved_boundary_path(ref, workspace_root)
        if resolved is not None and _is_same_path(target, resolved):
            return True
    return False


def _resolved_invocation_path(raw: object, workspace_root: Path) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = workspace_root / path
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _resolved_boundary_path(raw: object, workspace_root: Path) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = workspace_root / path
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_same_path(left: Path, right: Path) -> bool:
    return left.resolve(strict=False) == right.resolve(strict=False)


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
        return structured_tool_error(
            request.tool_name,
            _format_tool_exception(exc),
            "检查参数后重试;若反复失败,换一种方法或工具完成同一目标。",
            error_code="TOOL_ERROR",
        )


def _tool_params_with_runtime_boundary(request: AuthorizedToolDispatchRequest) -> dict[str, Any]:
    if not isinstance(request.write_boundary, dict):
        return request.tool_params
    params = dict(request.tool_params)
    if request.tool_name == "run_command":
        shell_mode = str(request.write_boundary.get("shell_access_mode") or "").strip()
        if shell_mode:
            params["__access_mode"] = shell_mode
    if request.tool_name == "read_artifact":
        _copy_boundary_path(
            BoundaryPathCopyRequest(
                params=params,
                boundary=request.write_boundary,
                target_key="__task_work_dir",
                source_key="task_work_dir",
            )
        )
    return params


def _copy_boundary_path(request: BoundaryPathCopyRequest) -> None:
    text = str(request.boundary.get(request.source_key) or "").strip()
    if text:
        request.params[request.target_key] = text


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
