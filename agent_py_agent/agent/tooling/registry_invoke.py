
from __future__ import annotations

"""Execute an authorized registry tool after parsing and auth checks."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts.tool_protocol_v2 import execution_payload_for_tool_protocol
from .controlled_exec import ControlledExecToolRequest, execute_controlled_exec_tool
from .models import (
    BaseTool,
    ToolAvailability,
    ToolExecutionResult,
    ToolInvocationContext,
    ToolRuntimeSnapshot,
)
from .tool_spec_schema import tool_spec_runtime_input_schema
from .write_boundary import WRITE_TOOL_NAMES, validate_write_boundary

_MAX_EXCEPTION_MESSAGE_CHARS = 500
_BOUNDARY_FILESYSTEM_TOOL_NAMES = WRITE_TOOL_NAMES | {
    "find_files",
    "list_files",
    "read_file",
    "search_text",
}
_SANDBOX_WRITE_BOUNDARY_TOOL_NAMES = {"run_command", "terminal_session", "lsp"}
_BOUNDARY_CONTEXT_TOOL_NAMES = _BOUNDARY_FILESYSTEM_TOOL_NAMES | _SANDBOX_WRITE_BOUNDARY_TOOL_NAMES
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
    owner_scope_root: str = ""
    runtime_snapshot: ToolRuntimeSnapshot | None = None
    owner_type: str = "main_agent"


@dataclass(frozen=True)
class AuthorizedToolDispatchRequest:
    tool_name: str
    tool: BaseTool
    tool_params: dict[str, Any]
    workspace_root: Path
    write_boundary: dict[str, object] | None
    invocation_context: ToolInvocationContext | None = None


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


# LLM: invoke 位于权限门之后，先复检无副作用 readiness，再进入任何参数归一、边界临时态或真实工具代码。
# 函数用途: 在同一请求快照下准备并执行已授权工具，同时把运行期掉线归一为 TOOL_UNAVAILABLE。
def invoke_registry_tool(request: RegistryToolInvokeRequest) -> ToolExecutionResult:
    tool = request.tools.get(request.tool_name)
    if tool is None:
        return structured_tool_error(
            request.tool_name,
            f"未知工具: {request.tool_name}",
            "查看本轮工具目录,使用其中列出的工具名;不要凭记忆猜测工具名。",
            error_code="TOOL_UNAVAILABLE",
        )

    availability = _runtime_tool_availability(tool)
    if not availability.available:
        return _tool_unavailable_result(request.tool_name, availability)

    tool_params = _tool_params_for_execution(
        request.payload,
        tool,
        request.allowed_tools,
    )
    tool_params = _with_task_workspace_relative_path(tool_params, request)
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
                invocation_context=ToolInvocationContext(
                    runtime_snapshot=_invocation_snapshot(request),
                ),
            )
        ),
    )


# LLM: readiness 异常按不可用处理，绝不能因为一个可选工具的 check 崩掉整个工具循环。
# 函数用途: 安全执行工具的无副作用可用性检查，供最终调用前实时复检。
def _runtime_tool_availability(tool: BaseTool) -> ToolAvailability:
    try:
        return tool.availability()
    except Exception as exc:
        return ToolAvailability.unavailable(
            f"availability check failed: {type(exc).__name__}"
        )


# LLM: readiness 错误只在授权通过后返回，原因不包含密钥、命令输出或外部响应正文。
# 函数用途: 把工具运行期不可用状态归一为稳定的 ToolExecutionResult。
def _tool_unavailable_result(
    tool_name: str,
    availability: ToolAvailability,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        tool_name,
        False,
        json.dumps(
            {
                "error": "tool_unavailable",
                "tool": tool_name,
                "reason": availability.reason or "当前运行环境未就绪",
            },
            ensure_ascii=False,
        ),
        error_code=availability.error_code or "TOOL_UNAVAILABLE",
    )


# LLM: 生产调用总会携带 registry 快照；兼容直调只按显式 allowlist 构造同语义的最小快照。
# 函数用途: 为 execute_scoped 提供不可变请求上下文，避免目录工具回读全局注册表。
def _invocation_snapshot(request: RegistryToolInvokeRequest) -> ToolRuntimeSnapshot:
    if request.runtime_snapshot is not None:
        return request.runtime_snapshot
    allowed = (
        None
        if request.allowed_tools is None
        else {str(item) for item in request.allowed_tools if str(item).strip()}
    )
    available_specs = []
    unavailable = []
    for name, tool in request.tools.items():
        if allowed is not None and name not in allowed:
            continue
        availability = _runtime_tool_availability(tool)
        if availability.available:
            available_specs.append(tool.spec)
        else:
            unavailable.append(
                (
                    name,
                    availability.error_code or "TOOL_UNAVAILABLE",
                    availability.reason,
                )
            )
    specs = tuple(available_specs)
    return ToolRuntimeSnapshot(
        specs=specs,
        available_tool_names=frozenset(spec.name for spec in specs),
        unavailable_tools=tuple(unavailable),
        allowed_tools=frozenset(allowed) if allowed is not None else None,
        owner_type=str(request.owner_type or "main_agent"),
    )


def _tool_params_for_execution(
    normalized_payload: dict[str, Any],
    tool: BaseTool,
    allowed_tools: list[str] | None,
) -> dict[str, Any]:
    schema = tool_spec_runtime_input_schema(tool.spec)
    properties = schema.get("properties")
    declared_fields = (
        tuple(str(key) for key in properties)
        if isinstance(properties, dict)
        else ()
    )
    canonical = execution_payload_for_tool_protocol(
        normalized_payload,
        declared_input_fields=declared_fields,
    )
    params = (
        dict(canonical.get("input") or {})
        if isinstance(canonical, dict)
        else {}
    )
    tool_name = tool.spec.name
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
        rewritten = _relocate_escape_abs_path(
            EscapeRelocateRequest(
                raw=raw,
                tool_name=request.tool_name,
                owner_scope_root=request.owner_scope_root,
                task_output_dir=(request.write_boundary or {}).get("task_output_dir"),
                # legal_roots = 写边界系统认可的全部合法根(工作区 + 所有已声明任务/边界根:
                #   task_output_dir、task_work_dir、task_dir、task_root、allowed_write_roots 等)。
                #   只有真正落在这些根之外的绝对路径才算"写飞"需归一——否则会误搬合法的
                #   task_work_dir 写入(如子代理 runner 写 output.json)。
                legal_roots=_workspace_roots_for_invocation(request) or [],
                dangerous_roots=request.path_dangerous_roots,
            )
        )
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


# 透明归一"写飞"绝对路径(F11①)的入参集束。字段打包成 dataclass 而非散参——既便于纯函数
# 单测,也避开参数个数 soft limit。legal_roots 是写边界系统认可的全部合法根(已解析 Path)。
@dataclass(frozen=True)
class EscapeRelocateRequest:
    raw: object
    tool_name: str
    owner_scope_root: object
    task_output_dir: object
    legal_roots: list[Path]
    dangerous_roots: list[str] | None


# 函数用途: 透明归一"写飞"的绝对路径(F11①)。
#   agent 用 write_file 写一个绝对路径到所有合法根之外的无害位置(如 /root/monitor_lab/x.py)时,
#   把它透明搬进当前任务的 task_output_dir 下、保留路径结构(去掉根锚),返回新路径——工具照常成功、
#   agent 无感。但越权(写别人 owner home)和危险目录(/etc 等)返回空 → 不归一,留给 owner 墙/
#   危险目录机制硬拦(归一会把硬拦变成静默成功,绝不能做)。落在任一合法根(工作区 + 已声明任务/
#   边界根)内、相对路径、读类工具、无任务上下文都返回空,交给现有机制处理。纯函数便于单测。
def _relocate_escape_abs_path(request: EscapeRelocateRequest) -> str:
    if request.tool_name not in WRITE_TOOL_NAMES:
        return ""
    target = _resolved_abs_path(request.raw)
    if target is None:
        return ""
    out_dir = _resolved_dir_path(request.task_output_dir)
    if out_dir is None:
        return ""
    scope = _resolved_dir_path(request.owner_scope_root)
    # 合法区内不动:目标已落在 task_output_dir、自己 owner home 或任一已声明合法根之下 → 不重写。
    legal_roots = [out_dir, *request.legal_roots]
    if scope is not None:
        legal_roots.append(scope)
    if any(_is_relative_to(target, root) for root in legal_roots):
        return ""
    # 越权不归一:写到别人 owner home(owners 根下但不属于自己 scope)→ 留给 owner 墙拦。
    if scope is not None:
        owners_root = _owners_root_for_scope(scope)
        if owners_root is not None and _is_relative_to(target, owners_root) and not _is_relative_to(target, scope):
            return ""
    # 危险目录不归一:留给危险目录机制拦。
    for root in _resolved_root_list(request.dangerous_roots):
        if _is_relative_to(target, root):
            return ""
    # 无害写飞:归一进 task_output_dir,保留结构去掉根锚(/root/monitor_lab/x.py → root/monitor_lab/x.py)。
    rel = target.relative_to(target.anchor)
    return str((out_dir / rel).resolve(strict=False))


def _resolved_abs_path(raw: object) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            return None
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _resolved_dir_path(raw: object) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _resolved_root_list(raw_roots: object) -> list[Path]:
    values = raw_roots if isinstance(raw_roots, list) else []
    roots: list[Path] = []
    for raw in values:
        resolved = _resolved_dir_path(raw)
        if resolved is not None:
            roots.append(resolved)
    return roots


def _owners_root_for_scope(scope: Path) -> Path | None:
    for parent in scope.parents:
        if parent.name == "owners":
            return parent
    return None


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
    if request.tool_name not in _BOUNDARY_CONTEXT_TOOL_NAMES or not isinstance(request.write_boundary, dict):
        return roots
    # LLM: mutating tools/shell 绝不能把 allowed_read_roots 当可写 workspace；只有纯读工具
    #   才扩展读取根。任务根和显式 write roots 是本轮可写结构化事实。
    # 人类: 读授权与写授权分开，避免“能读”被临时上下文升级成“能写”。
    keys = [
        "allowed_write_roots",
        "product_write_roots",
        "task_dir",
        "task_root",
        "task_output_dir",
        "task_work_dir",
    ]
    if (
        request.tool_name not in WRITE_TOOL_NAMES
        and request.tool_name not in _SANDBOX_WRITE_BOUNDARY_TOOL_NAMES
    ):
        keys.insert(0, "allowed_read_roots")
    for key in keys:
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


# LLM: 授权后的普通工具只有一个执行分支；scoped 默认委托 execute，目录工具才读取请求快照。
# 函数用途: 执行已通过权限和边界门的工具，并把实现异常统一收敛为结构化失败。
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
        params = _tool_params_with_runtime_boundary(request)
        if request.invocation_context is None:
            return request.tool.execute(params)
        return request.tool.execute_scoped(params, request.invocation_context)
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
    if request.tool_name in _SANDBOX_WRITE_BOUNDARY_TOOL_NAMES:
        shell_mode = str(request.write_boundary.get("shell_access_mode") or "").strip()
        if shell_mode and request.tool_name in {"run_command", "terminal_session"}:
            params["__access_mode"] = shell_mode
        raw_write_roots = request.write_boundary.get("allowed_write_roots")
        if isinstance(raw_write_roots, (list, tuple)):
            # 文件工具已有 validate_write_boundary；shell 内部的重定向/open/cp 无法从
            # command 文本安全解析，交给 bwrap 按同一结构化根做只读/可写挂载。
            params["__sandbox_write_roots"] = [
                str(item).strip() for item in raw_write_roots if str(item).strip()
            ]
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
