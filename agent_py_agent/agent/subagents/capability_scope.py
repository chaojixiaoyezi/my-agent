
# LLM: Capability routing may narrow a child request but never widen the manager's owner wall.
# Keep path normalization and owner-bound partitioning here so explicit, semantic, and lifecycle
# grant paths consume one structured rule instead of reimplementing permission prose.
# 模块用途: 汇总能力申请范围，并保证任何子代理授权都不能越过当前用户自己的目录边界。
from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .model_capabilities import CapabilityRequest
from .model_task import SubAgentTask
from .utils import _merge_list

if TYPE_CHECKING:
    from .services.lifecycle import RecordCapabilityGrantParams

_DELETE_COMMAND_REQUESTS = frozenset({"rm", "rmdir", "unlink"})


# LLM: This immutable decision is the only transport shape for owner-bound path checks; callers
# must not reconstruct allowed/rejected sets from prose.
# 类用途: 保存能力目录经过用户边界校验后的允许项、拒绝项和权威用户根目录。
@dataclass(frozen=True)
class CapabilityPathScopeDecision:
    """Structured owner-bound decision for requested capability paths."""

    allowed_paths: tuple[str, ...]
    rejected_paths: tuple[str, ...]
    owner_scope_root: str = ""


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
