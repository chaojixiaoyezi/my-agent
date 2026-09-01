
# LLM: Capability routing may narrow a child request but never widen the manager's owner wall
# or the direct parent's effective tool snapshot. Keep path/tool authority decisions here so
# explicit, semantic, and lifecycle grant paths consume one structured rule instead of prose.
# 模块用途: 汇总能力申请范围，并保证任何子代理授权都不能越过当前用户目录或直属父级工具上限。
from __future__ import annotations

import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .model_capabilities import CapabilityRequest
from .model_task import SubAgentTask
from .utils import _merge_list

if TYPE_CHECKING:
    from .services.lifecycle import RecordCapabilityGrantParams

_DELETE_COMMAND_REQUESTS = frozenset({"rm", "rmdir", "unlink"})
DIRECT_PARENT_TOOL_AUTHORITY_ATTR = "direct_parent_tool_authority"
_CREATION_TOOL_AUTHORITY: ContextVar[dict[str, object] | None] = ContextVar(
    "subagent_creation_tool_authority",
    default=None,
)


# LLM: This immutable decision is the only transport shape for owner-bound path checks; callers
# must not reconstruct allowed/rejected sets from prose.
# 类用途: 保存能力目录经过用户边界校验后的允许项、拒绝项和权威用户根目录。
@dataclass(frozen=True)
class CapabilityPathScopeDecision:
    """Structured owner-bound decision for requested capability paths."""

    allowed_paths: tuple[str, ...]
    rejected_paths: tuple[str, ...]
    owner_scope_root: str = ""


# LLM: This immutable snapshot is the only authority fact a parent-resolution model and the
# hard grant gate may share. It is derived from the exact parent run or root ToolRuntimeSnapshot,
# never from goal/problem/evidence prose or the child's claimed allowed_tools list.
# 类用途: 保存一条工具申请在直属父级当前权限内可授予和不可授予的精确工具集合。
@dataclass(frozen=True)
class CapabilityToolAuthorityDecision:
    """Structured direct-parent tool authority for one capability request."""

    parent_run_id: str
    authority_source: str
    requested_tools: tuple[str, ...]
    grantable_tools: tuple[str, ...]
    unavailable_tools: tuple[str, ...]
    snapshot_error_code: str = ""

    # LLM: Keep the model-visible shape versioned and machine-authored; callers must not add
    # inferred approval or completion claims around this payload.
    # 函数用途: 输出 capability wake、工具结果和审计共用的结构化父级权限快照。
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "capability_parent_tool_authority.v1",
            "parent_run_id": self.parent_run_id,
            "authority_source": self.authority_source,
            "requested_tools": list(self.requested_tools),
            "grantable_tools": list(self.grantable_tools),
            "unavailable_tools": list(self.unavailable_tools),
            "has_tool_request": bool(self.requested_tools),
            "all_requested_tools_grantable": bool(self.requested_tools) and not self.unavailable_tools,
            "snapshot_error_code": self.snapshot_error_code,
            "capability_resolution_requires_user_approval": False,
            "dangerous_tool_call_approval_is_separate": True,
        }


# LLM: This exception is the defense-in-depth boundary at canonical grant persistence. Routers
# should normally convert the same decision into a typed GAP before reaching this exception.
# 类用途: 表示某张能力授权试图越过用户目录硬墙，并携带可审计的拒绝路径。
class CapabilityOwnerScopeViolation(ValueError):
    # LLM: Preserve the typed decision on the exception for audits and tests; the message is only
    # diagnostic and must never be parsed to make an authorization decision.
    # 函数用途: 构造越界授权异常，并保留完整结构化路径裁决。
    def __init__(self, decision: CapabilityPathScopeDecision):
        self.decision = decision
        rejected = ",".join(decision.rejected_paths)
        super().__init__(f"CAPABILITY_PATH_OUTSIDE_OWNER_SCOPE:{rejected}")


# LLM: Only the path set that would be copied into a grant is authoritative here. cwd_scope is a
# fallback, matching scoped_grant_params; combining both would reject an unused descriptive field.
# 函数用途: 取得能力申请真正会进入授权账本的目录范围。
def effective_request_path_scope(request: CapabilityRequest) -> list[str]:
    values = request.path_scope or request.cwd_scope
    return list(dict.fromkeys(str(item or "").strip() for item in values if str(item or "").strip()))


# LLM: requested_tools and requested_mcp_tools are exact model-facing registry names. Do not
# fuzzy-match prefixes, capability prose, server labels, or tool descriptions into authority.
# 函数用途: 合并普通工具和 MCP 工具申请，得到父级授权硬门使用的唯一精确名称列表。
def requested_capability_tool_names(request: CapabilityRequest) -> list[str]:
    return _merge_list(
        list(getattr(request, "requested_tools", []) or []),
        list(getattr(request, "requested_mcp_tools", []) or []),
    )


# LLM: MCP aliases in either request field resolve only by an exact, unique leaf-name match inside
# the direct parent's authoritative snapshot. Move proven MCP names into the MCP audit field;
# ambiguous or absent names remain in their original field and therefore fail closed later.
# 函数用途: 规范普通/MCP 申请字段，把唯一命中的 MCP 短名归到完整 MCP 名，重名或未知时不猜。
def canonical_capability_tool_request_fields(
    agent_or_manager: Any,
    task: SubAgentTask,
    requested_tools: list[str] | tuple[str, ...],
    requested_mcp_tools: list[str] | tuple[str, ...],
) -> tuple[list[str], list[str]]:
    available, _source, _error_code = _direct_parent_available_tools(
        agent_or_manager,
        task,
    )
    ordinary: list[str] = []
    mcp: list[str] = []

    # LLM: A short MCP alias is safe only when one exact leaf exists inside this parent's
    # authoritative snapshot. Full but unavailable MCP names stay unresolved for the hard gate.
    # 函数用途: 在父级工具快照中解析唯一 MCP 短名；完整、重名或未知名称不做模糊猜测。
    def _unique_mcp_name(name: str) -> str:
        if name in available and name.startswith("mcp__"):
            return name
        if name.startswith("mcp__"):
            return ""
        candidates = sorted(
            item
            for item in available
            if item.startswith("mcp__") and item.rsplit("__", 1)[-1] == name
        )
        return candidates[0] if len(candidates) == 1 else ""

    for raw_name in requested_tools:
        name = str(raw_name or "").strip()
        if not name:
            continue
        resolved_mcp = _unique_mcp_name(name) if name not in available else ""
        if name.startswith("mcp__"):
            resolved_mcp = name
        target = mcp if resolved_mcp else ordinary
        resolved = resolved_mcp or name
        if resolved not in target:
            target.append(resolved)
    for raw_name in requested_mcp_tools:
        name = str(raw_name or "").strip()
        if not name:
            continue
        resolved = _unique_mcp_name(name) or name
        if resolved not in mcp:
            mcp.append(resolved)
    ordinary = [item for item in ordinary if item not in mcp]
    return ordinary, mcp


# LLM: Creation-time tool authority is copied from the exact immutable ToolRuntimeSnapshot
# carried by the Tool Gateway invocation. The child cannot supply or widen this host-owned field.
# 函数用途: 把父代理创建 child 当轮的真实工具上限压成可持久化快照，供跨进程能力裁决复用。
def creation_tool_authority_snapshot(runtime_snapshot: Any) -> dict[str, object]:
    return {
        "schema_version": "direct_parent_tool_authority.v1",
        "parent_run_id": str(getattr(runtime_snapshot, "run_id", "") or ""),
        "available_tool_names": sorted(
            str(item or "").strip()
            for item in (getattr(runtime_snapshot, "available_tool_names", ()) or ())
            if str(item or "").strip()
        ),
        "snapshot_hash": str(getattr(runtime_snapshot, "snapshot_hash", "") or ""),
        "owner_type": str(getattr(runtime_snapshot, "owner_type", "") or ""),
    }


# LLM: The scoped Tool Gateway handler binds creation authority through a ContextVar so concurrent
# tool calls cannot leak snapshots and internal host facts never pollute public/idempotency params.
# 函数用途: 在一次 create_subagents handler 内临时绑定父回合工具快照，退出时可靠恢复旧上下文。
@contextmanager
def bind_creation_tool_authority(runtime_snapshot: Any) -> Iterator[None]:
    token = _CREATION_TOOL_AUTHORITY.set(
        creation_tool_authority_snapshot(runtime_snapshot)
    )
    try:
        yield
    finally:
        _CREATION_TOOL_AUTHORITY.reset(token)


# LLM: Creation policy reads a defensive copy of the current host binding. No caller may mutate
# the ContextVar value after one child has persisted it.
# 函数用途: 读取当前派工工具调用绑定的父级权限快照；普通内部创建没有绑定时返回空对象。
def current_creation_tool_authority() -> dict[str, object]:
    return dict(_CREATION_TOOL_AUTHORITY.get() or {})


# LLM: Only a versioned creation snapshot whose parent id matches the canonical child edge is
# authoritative. Invalid or model-authored lookalikes fail closed and never become grant input.
# 函数用途: 从 child 状态读取创建它的父回合工具快照，并核对父子编号没有漂移。
def task_creation_tool_authority(task: SubAgentTask) -> dict[str, object]:
    attrs = getattr(task, "attributes", {})
    raw = attrs.get(DIRECT_PARENT_TOOL_AUTHORITY_ATTR) if isinstance(attrs, dict) else None
    snapshot = dict(raw) if isinstance(raw, dict) else {}
    if snapshot.get("schema_version") != "direct_parent_tool_authority.v1":
        return {}
    parent_run_id = str(snapshot.get("parent_run_id") or "").strip()
    expected_parent = str(getattr(task, "parent_id", "") or "").strip()
    if not parent_run_id or parent_run_id != expected_parent:
        return {}
    names = snapshot.get("available_tool_names")
    if not isinstance(names, list):
        return {}
    snapshot["available_tool_names"] = list(
        dict.fromkeys(str(item or "").strip() for item in names if str(item or "").strip())
    )
    return snapshot


# LLM: A nested parent can grant only its rebuilt effective execution-context tools, including
# prior ordinary/MCP grants and disabled-tool policy; a root parent uses its immutable runtime
# snapshot. Snapshot failure is fail-closed.
# 函数用途: 判断直属父级当前是否真的持有申请中的工具，供模型裁决事实和落账硬门共同使用。
def direct_parent_tool_authority(
    agent_or_manager: Any,
    task: SubAgentTask,
    requested_tools: list[str] | tuple[str, ...],
) -> CapabilityToolAuthorityDecision:
    requested = tuple(
        dict.fromkeys(
            str(item or "").strip()
            for item in requested_tools
            if str(item or "").strip()
        )
    )
    parent_run_id = str(getattr(task, "parent_id", "") or "").strip()
    available, source, error_code = _direct_parent_available_tools(
        agent_or_manager,
        task,
    )
    grantable = tuple(item for item in requested if item in available)
    unavailable = tuple(item for item in requested if item not in available)
    return CapabilityToolAuthorityDecision(
        parent_run_id=parent_run_id,
        authority_source=source,
        requested_tools=requested,
        grantable_tools=grantable,
        unavailable_tools=unavailable,
        snapshot_error_code=error_code,
    )


# LLM: Root and nested authority lookup is shared by hard grant checks, semantic-router bypass,
# and exact MCP alias canonicalization. Accept either the owning agent or its manager so the
# lower-level routing service does not grow a reverse dependency on agent-core.
# 函数用途: 读取直属父级当前真实工具集合，并返回来源与失败码供上层结构化记录。
def _direct_parent_available_tools(
    agent_or_manager: Any,
    task: SubAgentTask,
) -> tuple[set[str], str, str]:
    manager = getattr(agent_or_manager, "subagents", None)
    if manager is None and callable(getattr(agent_or_manager, "load", None)):
        manager = agent_or_manager
    parent_run_id = str(getattr(task, "parent_id", "") or "").strip()
    parent_task = None
    if parent_run_id and manager is not None and callable(getattr(manager, "load", None)):
        try:
            parent_task = manager.load(parent_run_id)
        except (FileNotFoundError, TypeError, ValueError):
            parent_task = None
    if parent_task is not None:
        try:
            parent_context = manager.runner_context.build_execution_context(parent_run_id)
            available = {
                str(item or "").strip()
                for item in (parent_context.allowed_tools or [])
                if str(item or "").strip()
            }
            source = "parent_run_execution_context"
            error_code = ""
        except Exception:
            available = set()
            source = "parent_run_execution_context"
            error_code = "PARENT_TOOL_SNAPSHOT_UNAVAILABLE"
    else:
        creation_snapshot = task_creation_tool_authority(task)
        if creation_snapshot:
            available = set(creation_snapshot["available_tool_names"])
            source = "parent_creation_runtime_snapshot"
            error_code = ""
        else:
            # 旧任务没有创建时快照；同 owner、同部署 registry 仅作显式兼容来源，新的
            # create_subagents 主链不会再走这里。失败时仍 fail closed。
            try:
                registry = agent_or_manager.tools
                snapshot = registry.runtime_snapshot(run_id=parent_run_id)
                available = set(snapshot.available_tool_names)
                source = "legacy_process_runtime_snapshot"
                error_code = "PARENT_CREATION_SNAPSHOT_MISSING"
            except Exception:
                available = set()
                source = "legacy_process_runtime_snapshot"
                error_code = "PARENT_TOOL_SNAPSHOT_UNAVAILABLE"
    return available, source, error_code


# LLM: owner_scope_root is a hard multi-tenant upper bound. Relative capability paths resolve from
# the exact child task workspace, never from the Gateway process cwd.
# 函数用途: 把申请目录分成用户自己家内可继续判断的路径和越过用户边界的拒绝路径。
def partition_capability_paths_by_owner(
    manager: Any,
    task: SubAgentTask,
    paths: list[str] | tuple[str, ...],
) -> CapabilityPathScopeDecision:
    cleaned = tuple(
        dict.fromkeys(str(item or "").strip() for item in paths if str(item or "").strip())
    )
    owner_text = str(getattr(manager, "owner_scope_root", "") or "").strip()
    if not owner_text:
        return CapabilityPathScopeDecision(cleaned, (), "")
    try:
        owner_root = Path(owner_text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return CapabilityPathScopeDecision((), cleaned, owner_text)
    allowed: list[str] = []
    rejected: list[str] = []
    for raw in cleaned:
        target = resolve_capability_scope_path(manager, task, raw)
        bucket = allowed if target is not None and _path_is_within(target, owner_root) else rejected
        bucket.append(raw)
    return CapabilityPathScopeDecision(tuple(allowed), tuple(rejected), str(owner_root))


# LLM: Canonical grant writers call this even if a router already checked the request. This keeps
# future or legacy callers from persisting an impossible grant that would create wake/retry loops.
# 函数用途: 在授权最终落账前复核目录上界，越界时拒绝整张授权。
def ensure_grant_path_scope_within_owner(
    manager: Any,
    task: SubAgentTask,
    paths: list[str] | tuple[str, ...],
) -> CapabilityPathScopeDecision:
    decision = partition_capability_paths_by_owner(manager, task, paths)
    if decision.rejected_paths:
        raise CapabilityOwnerScopeViolation(decision)
    return decision


# LLM: Path resolution is shared by router and explicit parent approval. Non-absolute input is
# task-local by contract; falling back to manager.workspace_root preserves local-unmanaged tests.
# 函数用途: 将能力范围里的路径按子代理任务目录解析成绝对路径。
def resolve_capability_scope_path(
    manager: Any,
    task: SubAgentTask,
    raw: str,
) -> Path | None:
    try:
        target = Path(raw).expanduser()
        if not target.is_absolute():
            base_text = str(
                getattr(task, "task_workspace_dir", "")
                or getattr(task, "task_dir", "")
                or getattr(manager, "workspace_root", "")
                or ""
            ).strip()
            if not base_text:
                return None
            target = Path(base_text).expanduser() / target
        return target.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


# LLM: This helper compares already normalized paths and has no filesystem mutation side effect.
# 函数用途: 判断绝对路径是否等于或位于给定根目录下。
def _path_is_within(path: Path, root: Path) -> bool:
    if path == root:
        return True
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def request_scope_snapshot(request: CapabilityRequest) -> dict[str, object]:
    return {
        "capability_type": request.capability_type,
        "requested_tools": list(request.requested_tools),
        "requested_skills": list(request.requested_skills),
        "requested_mcp_tools": list(request.requested_mcp_tools),
        "requested_commands": list(request.requested_commands),
        "cwd_scope": list(request.cwd_scope),
        "path_scope": list(request.path_scope),
        "network_scope": list(request.network_scope),
        "output_budget": dict(request.output_budget),
        "risk_level": request.risk_level,
        "alternatives_attempted": list(request.alternatives_attempted),
        "escalation_target": request.escalation_target,
    }


def scoped_constraints(request: CapabilityRequest) -> dict[str, str]:
    constraints = dict(request.constraints)
    if request.capability_type and request.capability_type != "generic":
        constraints.setdefault("capability_type", request.capability_type)
    if request.risk_level:
        constraints.setdefault("risk_level", request.risk_level)
    _set_csv_constraint(constraints, "requested_commands", request.requested_commands)
    _set_csv_constraint(constraints, "path_scope", request.path_scope)
    _set_csv_constraint(constraints, "network_scope", request.network_scope)
    return constraints


def grant_command_allowlist(request: CapabilityRequest) -> list[str]:
    return [
        item for item in dict.fromkeys(_requested_command_name(item) for item in request.requested_commands)
        if item and item.lower() not in _DELETE_COMMAND_REQUESTS
    ]


def existing_delete_trash_grant(task: SubAgentTask, request: CapabilityRequest):
    if not _delete_only_request(request):
        return None
    for grant in getattr(task, "capability_grants", []) or []:
        if _grant_supports_delete_trash(grant, request):
            return grant
    return None


def grant_tools(request: CapabilityRequest, routed_tools: list[str]) -> list[str]:
    return _merge_list(routed_tools, request.requested_tools)


def scoped_grant_params(
    request: CapabilityRequest,
    *,
    routed_skills: list[str],
    routed_tools: list[str],
    selected_cards: list[dict[str, str]],
    hit_count: int,
) -> RecordCapabilityGrantParams:
    from .services.lifecycle import RecordCapabilityGrantParams

    return RecordCapabilityGrantParams(
        request_id=request.id,
        skills=routed_skills,
        tools=grant_tools(request, routed_tools),
        mcp_tools=request.requested_mcp_tools,
        command_allowlist=grant_command_allowlist(request),
        grant_type=request.capability_type,
        capability_cards=selected_cards,
        reason=f"CapabilityRouter 命中 {hit_count} 张能力卡。",
        constraints=scoped_constraints(request),
        path_scope=request.path_scope or request.cwd_scope,
        network_scope=request.network_scope,
        output_budget=request.output_budget,
        risk_level=request.risk_level,
        request_scope=request_scope_snapshot(request),
        expires_after_task=True,
    )


def gap_attempted_tools(request: CapabilityRequest) -> list[str]:
    attempted = _merge_list(request.tried, request.requested_tools)
    return _merge_list(attempted, request.requested_commands)


def escalation_chain(task: SubAgentTask, request: CapabilityRequest) -> list[str]:
    chain = [
        request.escalation_target,
        task.supervisor,
        task.owner,
        task.parent_id,
        task.root_id,
    ]
    return [item for item in dict.fromkeys(chain) if item]


def _set_csv_constraint(target: dict[str, str], key: str, values: list[str]) -> None:
    cleaned = [value for value in values if value]
    if cleaned:
        target.setdefault(key, ",".join(cleaned))


def _requested_command_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    return str(parts[0]).strip() if parts else ""


def _delete_only_request(request: CapabilityRequest) -> bool:
    commands = [_requested_command_name(item).lower() for item in request.requested_commands]
    commands = [item for item in commands if item]
    return bool(commands) and all(item in _DELETE_COMMAND_REQUESTS for item in commands)


def _grant_supports_delete_trash(grant, request: CapabilityRequest) -> bool:
    tools = {str(item).strip() for item in getattr(grant, "tools", []) or []}
    if "controlled_exec" not in tools and str(getattr(grant, "grant_type", "") or "") != "shell":
        return False
    if not getattr(grant, "path_scope", None):
        return False
    return _path_scope_covers(getattr(grant, "path_scope", []), request.path_scope)


def _path_scope_covers(grant_scope: list[str], request_scope: list[str]) -> bool:
    requested = {str(item).strip() for item in request_scope if str(item).strip()}
    if not requested:
        return True
    granted = {str(item).strip() for item in grant_scope if str(item).strip()}
    return requested.issubset(granted)
