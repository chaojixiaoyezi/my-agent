
from __future__ import annotations

from typing import TYPE_CHECKING

from ..runtime_errors import runtime_error_report
from ..tooling.models import ToolExecutionResult
from .tool_targets import (
    actor_agent_id,
    collaboration_scope_payload,
    request_identity_report,
)
from .tool_values import dict_value, error, limit_param, ok, string_values

if TYPE_CHECKING:
    from ..core import SimpleAgent


def list_pending_requests(agent: SimpleAgent, params: dict[str, object]) -> ToolExecutionResult:
    identity, identity_error = request_identity_report(agent, params)
    if not any(identity.values()):
        return error("inspect_collaboration", "agent_identity_required", "agent_id, agent_name, agent_role, or current runner identity is required")
    try:
        requests, load_errors = agent.collaboration_store.pending_requests_for_agent_report(
            agent_id=identity["agent_id"],
            agent_name=identity["agent_name"],
            agent_role=identity["agent_role"],
            limit=limit_param(params.get("limit"), default=10),
        )
    except Exception as exc:
        return error(
            "inspect_collaboration",
            "pending_requests_read_failed",
            "pending collaboration request lookup failed",
            _load_error(exc, "inspect_collaboration.pending_requests"),
        )
    payload = {**identity, "request_count": len(requests), "requests": requests}
    if load_errors:
        payload["load_errors"] = load_errors
    if identity_error:
        payload["identity_load_error"] = identity_error
    payload.update(collaboration_scope_payload(agent, params, explicit_keys=("agent_id", "run_id")))
    return ok("inspect_collaboration", payload)


def update_request(agent: SimpleAgent, tool: str, params: dict[str, object], *, targets: list[str]) -> ToolExecutionResult:
    return _update_request_tool(agent, tool, params, targets=targets)


def _update_request_tool(agent: SimpleAgent, tool: str, params: dict[str, object], *, targets: list[str]) -> ToolExecutionResult:
    ids = _case_and_request_ids(params)
    if isinstance(ids, ToolExecutionResult):
        return ids
    case_id, request_id = ids
    try:
        request = agent.collaboration_store.update_request_status(
            {
                "case_id": case_id,
                "request_id": request_id,
                "status": str(params.get("status") or "pending"),
                "actor_agent_id": actor_agent_id(agent, params),
                "summary": str(params.get("summary") or ""),
                "target_agent_ids": targets,
                "metadata": dict_value(params.get("metadata")),
            }
        )
    except (KeyError, ValueError) as exc:
        return error(tool, "request_update_failed", str(exc))
    payload = {"request": request.to_dict(), "overview": _request_overview(agent, case_id)}
    payload.update(collaboration_scope_payload(agent, params, explicit_keys=("actor_agent_id", "agent_id", "run_id")))
    return ok(tool, payload)


def _case_and_request_ids(params: dict[str, object]) -> tuple[str, str] | ToolExecutionResult:
    case_id = str(params.get("case_id") or "").strip()
    request_id = str(params.get("request_id") or "").strip()
    if not case_id:
        return error("update_collaboration", "case_id_required", "case_id is required")
    if not request_id:
        return error("update_collaboration", "request_id_required", "request_id is required")
    return case_id, request_id


def _request_overview(agent: SimpleAgent, case_id: str) -> dict[str, object]:
    try:
        status = agent.collaboration_store.case_status(case_id)
    except Exception as exc:
        return {"overview_load_error": runtime_error_report(exc, context="update_collaboration.case_status")}
    return {key: status[key] for key in ("pending_request_count", "blocked_request_count", "timed_out_request_count", "completed_request_count", "ready_for_main_agent")}


def _load_error(exc: BaseException, context: str) -> dict[str, object]:
    return {"load_error": runtime_error_report(exc, context=context)}
