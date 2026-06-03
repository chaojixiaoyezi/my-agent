
from __future__ import annotations

"""Scoped capability routing helpers."""

import shlex

from .model_capabilities import CapabilityRequest
from .model_task import SubAgentTask
from .services.lifecycle import RecordCapabilityGrantParams
from .utils import _merge_list

_DELETE_COMMAND_REQUESTS = frozenset({"rm", "rmdir", "unlink"})


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
        "fallback_attempted": list(request.fallback_attempted),
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
        expires_after_task=True,
        reserved={"request_scope": request_scope_snapshot(request)},
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
