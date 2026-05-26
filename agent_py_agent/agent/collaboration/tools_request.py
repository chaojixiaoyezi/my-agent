# LLM: Request-level collaboration tools create, discover, update, and reroute requests.
# 模块用途: 实现 request_collaboration、list_collaboration_requests、update 和 reroute 工具。

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..tools import BaseTool, ToolExecutionResult
from .tool_specs import (
    build_list_collaboration_requests_spec,
    build_request_collaboration_spec,
    build_reroute_collaboration_request_spec,
    build_update_collaboration_request_spec,
)
from .tool_targets import (
    request_identity,
    resolved_target_agent_ids,
    target_response_payload,
    target_runtime_metadata,
    target_runtime_summary,
)
from .tool_values import deadline_at, dict_value, dict_values, error, limit_param, ok, string_values

if TYPE_CHECKING:
    from ..core import SimpleAgent


@dataclass(frozen=True)
class _RequestBuildInput:
    agent: SimpleAgent
    params: dict[str, object]
    targets: list[str]
    runtime: dict[str, object]


class RequestCollaborationTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_request_collaboration_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        case_id = str(params.get("case_id") or "").strip()
        if not case_id:
            return error("request_collaboration", "case_id_required", "case_id is required")
        targets = resolved_target_agent_ids(self.agent, params)
        runtime = target_runtime_summary(self.agent, targets)
        request = self.agent.collaboration_store.request_collaboration({"case_id": case_id, **_request_kwargs(_RequestBuildInput(self.agent, params, targets, runtime))})
        runtime = target_runtime_summary(self.agent, list(request.target_agent_ids))
        return ok("request_collaboration", {"case_id": case_id, "request_id": request.request_id, "target_agent_ids": list(request.target_agent_ids), **target_response_payload(runtime)})


class ListCollaborationRequestsTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_list_collaboration_requests_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        identity = request_identity(self.agent, params)
        if not any(identity.values()):
            return error("list_collaboration_requests", "agent_identity_required", "agent_id, agent_name, agent_role, or current runner identity is required")
        requests = self.agent.collaboration_store.pending_requests_for_agent(agent_id=identity["agent_id"], agent_name=identity["agent_name"], agent_role=identity["agent_role"], limit=limit_param(params.get("limit"), default=10))
        return ok("list_collaboration_requests", {**identity, "request_count": len(requests), "requests": requests})


class UpdateCollaborationRequestTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_update_collaboration_request_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        return _update_request_tool(self.agent, "update_collaboration_request", params, targets=string_values(params.get("target_agent_ids")))


class RerouteCollaborationRequestTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_reroute_collaboration_request_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        targets = string_values(params.get("target_agent_ids"))
        if not targets:
            return error("reroute_collaboration_request", "target_agent_ids_required", "target_agent_ids is required")
        return _update_request_tool(self.agent, "reroute_collaboration_request", params, targets=targets)


def _request_kwargs(request: _RequestBuildInput) -> dict[str, object]:
    params = request.params
    return {
        "requester_agent_id": str(params.get("requester_agent_id") or ""),
        "required_capabilities": string_values(params.get("required_capabilities")),
        "question": str(params.get("question") or ""),
        "entities": dict_value(params.get("entities")),
        "problem_statement": str(params.get("problem_statement") or ""),
        "observed_facts": dict_values(params.get("observed_facts")),
        "query_intent": dict_value(params.get("query_intent")),
        "query_hints": dict_values(params.get("query_hints")),
        "routing_requirements": dict_value(params.get("routing_requirements")),
        "response_contract": dict_value(params.get("response_contract")),
        "context_refs": string_values(params.get("context_refs")),
        "target_agent_ids": request.targets,
        "deadline_at": deadline_at(request.agent, params),
        "priority": str(params.get("priority") or ""),
        "metadata": {**dict_value(params.get("metadata")), **target_runtime_metadata(request.runtime)},
    }


def _update_request_tool(agent: SimpleAgent, tool: str, params: dict[str, object], *, targets: list[str]) -> ToolExecutionResult:
    ids = _case_and_request_ids(params)
    if isinstance(ids, ToolExecutionResult):
        return ids
    case_id, request_id = ids
    try:
        request = agent.collaboration_store.update_request_status({'case_id': case_id, 'request_id': request_id, 'status': str(params.get("status") or "pending"), 'actor_agent_id': str(params.get("actor_agent_id") or ""), 'summary': str(params.get("summary") or ""), 'target_agent_ids': targets, 'metadata': dict_value(params.get("metadata"))})
    except (KeyError, ValueError) as exc:
        return error(tool, "request_update_failed", str(exc))
    return ok(tool, {"request": request.to_dict(), "overview": _request_overview(agent, case_id)})


def _case_and_request_ids(params: dict[str, object]) -> tuple[str, str] | ToolExecutionResult:
    case_id = str(params.get("case_id") or "").strip()
    request_id = str(params.get("request_id") or "").strip()
    if not case_id:
        return error("update_collaboration_request", "case_id_required", "case_id is required")
    if not request_id:
        return error("update_collaboration_request", "request_id_required", "request_id is required")
    return case_id, request_id


def _request_overview(agent: SimpleAgent, case_id: str) -> dict[str, object]:
    status = agent.collaboration_store.case_status(case_id)
    return {key: status[key] for key in ("pending_request_count", "blocked_request_count", "timed_out_request_count", "completed_request_count", "ready_for_main_agent")}
