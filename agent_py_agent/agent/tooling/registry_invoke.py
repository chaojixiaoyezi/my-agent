
from __future__ import annotations

"""Execute an authorized registry tool after parsing and auth checks."""

import json
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..concurrency.interrupt import register_interrupt_callback
from ..path_access_policy import PathAccessPolicy, inheritable_declared_work_roots
from .cancellation import ToolCancelled, bind_cancellation_token
from .controlled_exec import ControlledExecToolRequest, execute_controlled_exec_tool
from .models import (
    BaseTool,
    ToolFailureStage,
    ToolHandlerOutcome,
    ToolInvocationContext,
    ToolRuntime,
    ToolRuntimeSnapshot,
    apply_tool_execution_facts,
)
from .registry_workspace import effective_registry_cwd
from .runtime_boundary import (
    canonicalize_owner_home_arguments,
    exact_read_boundary_error,
)
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


# LLM: This request is the immutable per-invocation permission snapshot. Shared registry handlers
# must never be mutated from its workspace, owner, or private-network fields.
# 类用途: 将一次工具调用的参数、工作区、owner 墙和运行快照固定在一起。
@dataclass(frozen=True)
class RegistryToolInvokeRequest:
    tool_name: str
    arguments: dict[str, Any]
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
    cancellation_token: object | None = None
    # Canonical executor supplies the exact immutable binding selected from the
    # run snapshot. Missing bindings fail closed; this is not a public adapter.
    runtime: ToolRuntime | None = None


@dataclass(frozen=True)
class AuthorizedToolDispatchRequest:
    tool_name: str
    tool: BaseTool
    tool_params: dict[str, Any]
    workspace_root: Path
    write_boundary: dict[str, object] | None
    sandbox_read_roots: tuple[Path, ...] = ()
    invocation_context: ToolInvocationContext | None = None


@dataclass(frozen=True)
class BoundaryPathCopyRequest:
    params: dict[str, Any]
    boundary: dict[str, object]
    target_key: str
    source_key: str


# 函数用途: 组一条结构化工具错误(批3 长期助手 范式):error 说出了什么错,
#   hint 给可操作下一步,JSON 形态便于模型解析,不再裸文本。
def structured_tool_error(tool_name: str, error: str, hint: str, *, error_code: str) -> ToolHandlerOutcome:
    payload = json.dumps({"error": error, "hint": hint}, ensure_ascii=False)
    return ToolHandlerOutcome(tool_name, False, payload, error_code=error_code)


# 函数用途: 写边界校验(越界返回拒绝结果,合规返回 None)。
def _write_boundary_denied(
    request: RegistryToolInvokeRequest, tool_params: dict[str, Any], workspace_roots: tuple[Path, ...]
) -> ToolHandlerOutcome | None:
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
    return ToolHandlerOutcome(request.tool_name, False, boundary_error, error_code="WRITE_FORBIDDEN")


# LLM: Exact read scope is enforced before any filesystem handler runs.  The
# owner policy remains a broad outer fence; it cannot substitute for a task's
# narrower, explicitly granted source view.
# 函数用途: 精确读取模式下只放行 allowed_read_roots 本身或其子路径，越界时无副作用拒绝。
def _read_boundary_denied(
    request: RegistryToolInvokeRequest,
    tool_params: dict[str, Any],
) -> ToolHandlerOutcome | None:
    error = exact_read_boundary_error(
        request.tool_name,
        tool_params,
        workspace_root=request.workspace_root,
        write_boundary=request.write_boundary,
    )
    if not error:
        return None
    return structured_tool_error(
        request.tool_name,
        "read_scope_forbidden",
        error,
        error_code="TOOL_PERMISSION_DENIED",
    )


# LLM: invoke 位于权限门之后，先复检无副作用 readiness，再进入任何参数归一、边界临时态或真实工具代码。
# 函数用途: 在同一请求快照下准备并执行已授权工具，同时把运行期掉线归一为 TOOL_UNAVAILABLE。
def invoke_registry_tool(request: RegistryToolInvokeRequest) -> ToolHandlerOutcome:
    request = _with_effective_registry_workspace(request)
    runtime = request.runtime
    if runtime is None and request.runtime_snapshot is not None:
        runtime = request.runtime_snapshot.runtime(request.tool_name)
    if runtime is None or request.runtime_snapshot is None:
        return apply_tool_execution_facts(
            structured_tool_error(
                request.tool_name,
                "canonical runtime binding is missing",
                "只能从本轮 ToolRuntimeSnapshot 选择工具；请重新建立运行快照。",
                error_code="TOOL_NOT_IN_RUNTIME_SNAPSHOT",
            ),
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
    tool = runtime.handler

    tool_params = _tool_params_for_execution(
        request.tool_name,
        request.arguments,
        request.allowed_tools,
    )
    tool_params = _with_task_workspace_relative_path(tool_params, request)
    read_denied = _read_boundary_denied(request, tool_params)
    if read_denied is not None:
        return apply_tool_execution_facts(
            read_denied,
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
    workspace_roots = _workspace_roots_for_invocation(request)
    sandbox_read_roots = _sandbox_read_roots_for_invocation(request)
    not_ready = _active_child_output_not_ready_result(request, tool_params)
    if not_ready is not None:
        return apply_tool_execution_facts(
            not_ready,
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
    boundary_denied = _write_boundary_denied(request, tool_params, workspace_roots)
    if boundary_denied is not None:
        return apply_tool_execution_facts(
            boundary_denied,
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )

    execution_tool = _request_local_tool_for_invocation(
        tool,
        request=request,
        workspace_roots=workspace_roots,
    )
    return _execute_with_temporary_tool_context(
        execution_tool,
        workspace_roots=workspace_roots,
        allowed_private_hosts=_boundary_string_tuple(request.write_boundary, "allowed_private_hosts"),
        allow_private_resolution=_boundary_bool(request.write_boundary, "allow_private_resolution"),
        callback=lambda: execute_authorized_tool(
            AuthorizedToolDispatchRequest(
                tool_name=request.tool_name,
                tool=execution_tool,
                tool_params=tool_params,
                workspace_root=request.workspace_root,
                write_boundary=request.write_boundary,
                sandbox_read_roots=sandbox_read_roots,
                invocation_context=ToolInvocationContext(
                    runtime_snapshot=_invocation_snapshot(request),
                    cancellation_token=request.cancellation_token,
                ),
            )
        ),
    )


def _with_effective_registry_workspace(
    request: RegistryToolInvokeRequest,
) -> RegistryToolInvokeRequest:
    """Apply the typed per-run cwd before any path validation or execution."""

    selected = effective_registry_cwd(request.workspace_root, request.write_boundary)
    if selected == Path(request.workspace_root).resolve(strict=False):
        return request
    return replace(request, workspace_root=selected)


# LLM: Registry handlers are shared across concurrent TUI requests. Any request-scoped workspace
# or private-network field must be applied to a shallow copy; Terminal also needs its nested
# ShellTool copied so one request cannot mutate another request's permissions.
# 函数用途: 为本次工具调用复制带可变权限字段的 handler，避免单 Gateway 并发串目录或私网授权。
def _request_local_tool_for_invocation(
    tool: BaseTool,
    *,
    request: RegistryToolInvokeRequest,
    workspace_roots: list[Path] | None,
) -> BaseTool:
    """Return a request-local handler view without mutating the shared registry."""

    terminal_shell = (
        getattr(tool, "shell_tool", None)
        if request.tool_name == "terminal_session"
        else None
    )
    needs_workspace_copy = request.tool_name in _BOUNDARY_CONTEXT_TOOL_NAMES and (
        hasattr(tool, "workspace_root") or hasattr(terminal_shell, "workspace_root")
    )
    needs_network_copy = hasattr(tool, "allowed_private_hosts") or hasattr(
        tool, "allow_private_resolution"
    )
    if not needs_workspace_copy and not needs_network_copy:
        return tool
    scoped = copy(tool)
    if request.tool_name == "terminal_session" and hasattr(scoped, "shell_tool"):
        scoped.shell_tool = copy(scoped.shell_tool)
    if needs_workspace_copy:
        workspace_target = (
            scoped.shell_tool
            if request.tool_name == "terminal_session" and hasattr(scoped, "shell_tool")
            else scoped
        )
        workspace_target.workspace_root = request.workspace_root
        if workspace_roots is not None and hasattr(workspace_target, "workspace_roots"):
            workspace_target.workspace_roots = list(workspace_roots)
    configured_policy = getattr(
        scoped.shell_tool
        if request.tool_name == "terminal_session" and hasattr(scoped, "shell_tool")
        else scoped,
        "path_access_policy",
        None,
    )
    # 请求可以把 owner 墙收得更窄，但缺失的请求字段绝不能擦掉注册时已有的
    # owner 墙。生产调用通常显式携带该值；保留更严格的 handler 配置可让低层
    # 调用、旧快照或不完整自定义入口继续 fail closed，而不会变成跨 owner 读取。
    effective_owner_scope = request.owner_scope_root or getattr(
        configured_policy, "owner_scope_root", ""
    )
    dynamic_policy = PathAccessPolicy.from_values(
        mode=request.path_access_mode,
        dangerous_roots=request.path_dangerous_roots,
        owner_scope_root=effective_owner_scope,
    )
    if hasattr(scoped, "path_access_policy"):
        scoped.path_access_policy = dynamic_policy
    if request.tool_name == "terminal_session" and hasattr(scoped, "shell_tool"):
        scoped.shell_tool.path_access_policy = dynamic_policy
    # LLM: owner 墙以外的硬拦（危险目录、凭据文件）仍由 policy 裁决；这里只把"宿主本轮已授权的
    #   墙外工作根"单独交给 handler，让它和中央路径门用同一份授权事实。
    # 人类: 只能由 registry 逐次下发，模型参数与 workspace_roots 都不能产生这个白名单。
    if hasattr(scoped, "granted_external_roots"):
        scoped.granted_external_roots = _granted_external_work_roots(request, effective_owner_scope)
    if (
        request.tool_name == "terminal_session"
        and hasattr(scoped.shell_tool, "granted_external_roots")
    ):
        scoped.shell_tool.granted_external_roots = _granted_external_work_roots(
            request, effective_owner_scope
        )
    return scoped


# LLM: 生产调用总会携带 registry 快照；缺失快照绝不能按进程级工具表重建权限。
# 函数用途: 为 execute_scoped 提供不可变请求上下文，避免目录工具回读全局注册表。
def _invocation_snapshot(request: RegistryToolInvokeRequest) -> ToolRuntimeSnapshot:
    if request.runtime_snapshot is not None:
        return request.runtime_snapshot
    raise RuntimeError("canonical tool invocation requires a runtime snapshot")


def _tool_params_for_execution(
    tool_name: str,
    arguments: dict[str, Any],
    allowed_tools: list[str] | None,
) -> dict[str, Any]:
    """Project already-validated canonical arguments into the handler seam."""

    params = dict(arguments)
    if tool_name == "read_file" and allowed_tools is not None:
        params["__allowed_tools"] = list(allowed_tools)
    return params


def _with_task_workspace_relative_path(
    params: dict[str, Any],
    request: RegistryToolInvokeRequest,
) -> dict[str, Any]:
    return canonicalize_owner_home_arguments(
        request.tool_name,
        params,
        request.write_boundary,
    )


# LLM: 活跃 child 的声明产物尚未就绪时只允许等待生命周期事件；不得轮询内部
# 文件或返回模型可执行的查询工具调用。
# 函数用途: 为尚在生成中的子代理产物返回可恢复、无副作用的读取失败。
def _active_child_output_not_ready_result(
    request: RegistryToolInvokeRequest,
    params: dict[str, Any],
) -> ToolHandlerOutcome | None:
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
        "next_action": "await_direct_child_lifecycle_event",
        "result_fields_to_read": ["child_result_index.read_order", "child_result_index.primary_artifact_refs"],
    }
    return ToolHandlerOutcome(
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


# Keep the low-level filesystem tools aligned with the current task workspace roots.
def _workspace_roots_for_invocation(request: RegistryToolInvokeRequest) -> list[Path] | None:
    roots = _normalized_roots(request.workspace_root, request.workspace_roots)
    if request.tool_name not in _BOUNDARY_CONTEXT_TOOL_NAMES or not isinstance(request.write_boundary, dict):
        return roots
    # LLM: 文件写工具绝不能把 allowed_read_roots 当可写 workspace。进程工具可以把
    #   只读根作为 cwd/输入，但真实写权限仍只由传给 bwrap 的 write roots 决定。
    # 人类: shell 可以在只读源码目录里跑测试，却不能因为能 cd 进去就改写源码。
    keys = [
        "allowed_write_roots",
        "product_write_roots",
        "task_dir",
        "task_root",
        "task_output_dir",
        "task_work_dir",
    ]
    if request.tool_name not in WRITE_TOOL_NAMES:
        keys[:0] = ["allowed_read_roots", "owner_workspace_dir"]
    for key in keys:
        _append_boundary_roots(roots, request.write_boundary.get(key), request.workspace_root)
    return roots


def _sandbox_read_roots_for_invocation(
    request: RegistryToolInvokeRequest,
) -> tuple[Path, ...]:
    """Return the exact read view granted to owner-scoped process tools."""
    if (
        request.tool_name not in _SANDBOX_WRITE_BOUNDARY_TOOL_NAMES
        or not isinstance(request.write_boundary, dict)
    ):
        return ()
    roots = _normalized_roots(request.workspace_root, request.workspace_roots)
    for key in ("allowed_read_roots", "owner_workspace_dir"):
        _append_boundary_roots(
            roots,
            request.write_boundary.get(key),
            request.workspace_root,
        )
    return tuple(roots)


def _execute_with_temporary_tool_context(
    tool: BaseTool,
    *,
    workspace_roots: list[Path] | None,
    allowed_private_hosts: tuple[str, ...],
    allow_private_resolution: bool | None,
    callback: Callable[[], ToolHandlerOutcome],
) -> ToolHandlerOutcome:
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


# LLM: "墙外已授权工作根"是宿主事实：只从 write_boundary 的 allowed_write_roots /
#   product_write_roots 派生，落在 owner 墙内的不算（墙内本来就走 owner 语义），
#   系统根目录、宿主控制面、其它 owner 的家由 inheritable_declared_work_roots 过滤掉。
#   handler 的 owner 墙逃生口只认这份列表，不认 workspace_roots，避免"改一个可变列表就放权"。
# 函数用途: 给逐次调用的 handler 计算可以穿过 owner 墙的已授权工作根。
def _granted_external_work_roots(
    request: RegistryToolInvokeRequest,
    owner_scope: str,
) -> tuple[Path, ...]:
    if not str(owner_scope or "").strip() or not isinstance(request.write_boundary, dict):
        return ()
    owner_root = Path(str(owner_scope)).expanduser().resolve(strict=False)
    values: list[object] = []
    for key in ("allowed_write_roots", "product_write_roots"):
        raw = request.write_boundary.get(key)
        if isinstance(raw, (list, tuple)):
            values.extend(raw)
    granted: list[Path] = []
    for item in inheritable_declared_work_roots(values, owner_home=owner_root):
        root = Path(str(item)).expanduser().resolve(strict=False)
        if root == owner_root or _is_under(root, owner_root):
            continue
        if root not in granted:
            granted.append(root)
    return tuple(granted)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


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
def execute_authorized_tool(request: AuthorizedToolDispatchRequest) -> ToolHandlerOutcome:
    if request.invocation_context is None:
        return apply_tool_execution_facts(
            structured_tool_error(
                request.tool_name,
                "canonical invocation context is missing",
                "只能由 ToolExecutor 使用当前 ToolRuntimeSnapshot 进入 handler。",
                error_code="TOOL_NOT_IN_RUNTIME_SNAPSHOT",
            ),
            failure_stage=ToolFailureStage.RUNTIME_GATE,
            handler_executed=False,
        )
    try:
        token = request.invocation_context.cancellation_token
        cancel = getattr(token, "cancel", None)
        interrupt_callback = (
            (lambda: cancel("interrupted")) if callable(cancel) else (lambda: None)
        )
        with bind_cancellation_token(token), register_interrupt_callback(
            interrupt_callback
        ):
            if request.tool_name == "controlled_exec":
                result = execute_controlled_exec_tool(
                    ControlledExecToolRequest(
                        params=request.tool_params,
                        workspace_root=request.workspace_root,
                        write_boundary=request.write_boundary,
                    )
                )
            else:
                params = _tool_params_with_runtime_boundary(request)
                result = request.tool.execute_scoped(params, request.invocation_context)
    except ToolCancelled as exc:
        result = structured_tool_error(
            request.tool_name,
            str(exc) or "cancelled",
            "停止派发新动作并保留已有结构化结果。",
            error_code="CANCELLED",
        )
    except Exception as exc:
        result = structured_tool_error(
            request.tool_name,
            _format_tool_exception(exc),
            "检查参数后重试;若反复失败,换一种方法或工具完成同一目标。",
            error_code="TOOL_ERROR",
        )
    return apply_tool_execution_facts(
        result,
        failure_stage=(
            None
            if result.ok or result.failure_stage
            else ToolFailureStage.EXECUTION
        ),
        handler_executed=True,
    )


# LLM: 内部运行参数只投影结构化 boundary；sandbox 写根的集合不变，但 task_work_dir 必须排在首位，
# 供 Linux attempt 沙箱选择独立持久临时根，不能让 working_dir/项目目录承担临时缓存。
# 函数用途: 给已授权工具补充模型不可见的写根、读根、临时根顺序和其它运行边界参数。
def _tool_params_with_runtime_boundary(request: AuthorizedToolDispatchRequest) -> dict[str, Any]:
    if not isinstance(request.write_boundary, dict):
        return request.tool_params
    params = dict(request.tool_params)
    if request.tool_name in _SANDBOX_WRITE_BOUNDARY_TOOL_NAMES:
        shell_mode = str(request.write_boundary.get("shell_access_mode") or "").strip()
        if shell_mode and request.tool_name in {"run_command", "terminal_session"}:
            params["__access_mode"] = shell_mode
        params["__sandbox_read_roots"] = [
            str(root) for root in request.sandbox_read_roots
        ]
        if request.tool_name in {"run_command", "terminal_session"}:
            params["__sandbox_protected_write_paths"] = _boundary_path_strings(
                request.write_boundary,
                "forbidden_write_roots",
            )
        raw_write_roots = request.write_boundary.get("allowed_write_roots")
        if isinstance(raw_write_roots, (list, tuple)):
            # 文件工具已有 validate_write_boundary；shell 内部的重定向/open/cp 无法从
            # command 文本安全解析，交给 bwrap 按同一结构化根做只读/可写挂载。
            params["__sandbox_write_roots"] = _ordered_sandbox_write_roots(
                request.write_boundary,
                raw_write_roots,
            )
        elif request.tool_name in {"run_command", "terminal_session"}:
            # owner-scoped 进程一旦携带结构化 boundary，漏写 allowed_write_roots
            # 只能解释为“没有授权写根”，不能回落成 cwd 隐式可写。没有 boundary 的
            # WorkspaceOnly 主会话仍沿既有 owner-home 语义；无 owner 墙的 Full Access
            # 也继续由宿主权限决定，二者都不会进入这个 fail-closed 分支。
            sandbox_tool = (
                getattr(request.tool, "shell_tool", request.tool)
                if request.tool_name == "terminal_session"
                else request.tool
            )
            path_policy = getattr(sandbox_tool, "path_access_policy", None)
            if getattr(path_policy, "owner_scope_root", None) is not None:
                params["__sandbox_write_roots"] = []
    if request.tool_name == "read_artifact":
        _copy_boundary_path(
            BoundaryPathCopyRequest(
                params=params,
                boundary=request.write_boundary,
                target_key="__artifact_read_root",
                source_key="artifact_read_root",
            )
        )
        if "__artifact_read_root" not in params:
            _copy_boundary_path(
                BoundaryPathCopyRequest(
                    params=params,
                    boundary=request.write_boundary,
                    target_key="__artifact_read_root",
                    source_key="task_work_dir",
                )
            )
        scope_mode = str(
            request.write_boundary.get("artifact_read_scope_mode") or ""
        ).strip()
        if scope_mode:
            params["__artifact_read_scope_mode"] = scope_mode
    return params


# LLM: Internal sandbox paths come only from the already-authenticated host boundary. Keep their
# order stable for reproducible permission snapshots and omit malformed scalar values.
# 函数用途: 把边界里的路径列表规范成传给进程沙箱的字符串数组。
def _boundary_path_strings(boundary: dict[str, object], key: str) -> list[str]:
    raw = boundary.get(key)
    if not isinstance(raw, (list, tuple)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


# LLM: 第一项只承担 /tmp 挂载根的选择，不扩大 allowed_write_roots 集合；只有 task_work_dir
# 与现有允许根精确相同时才前置，畸形或越界 boundary 保持原顺序并交给后续门禁裁决。
# 函数用途: 把任务 work 根排到沙箱写根首位，确保项目子目录不会生成 .sandbox-tmp。
def _ordered_sandbox_write_roots(
    boundary: dict[str, object],
    raw_roots: list[object] | tuple[object, ...],
) -> list[str]:
    roots = list(
        dict.fromkeys(str(item).strip() for item in raw_roots if str(item).strip())
    )
    task_work_dir = str(boundary.get("task_work_dir") or "").strip()
    if task_work_dir in roots:
        roots.remove(task_work_dir)
        roots.insert(0, task_work_dir)
    return roots


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
