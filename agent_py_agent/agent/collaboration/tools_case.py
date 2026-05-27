# LLM: Case-level collaboration tools expose generic case lifecycle actions.
# 模块用途: 实现 open_case、raise_collaboration_event、update_case_status 和 case_status 工具。

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..tools import BaseTool, ToolExecutionResult
from .tool_specs import (
    build_case_status_spec,
    build_open_case_spec,
    build_raise_collaboration_event_spec,
    build_update_case_status_spec,
)
from .tool_targets import (
    actor_agent_id,
    collaboration_scope_payload,
    resolved_target_agent_ids,
    target_response_payload,
    target_runtime_metadata,
    target_runtime_summary,
)
from .tool_thread import resolve_thread
from .tool_values import deadline_at, dict_value, dict_values, error, ok, string_values

if TYPE_CHECKING:
    from ..core import SimpleAgent


@dataclass(frozen=True)
class _CasePayloadRefs:
    thread_id: str
    task_id: str
    params: dict[str, object]


@dataclass(frozen=True)
class _EventCaseInput:
    thread_id: str
    task_id: str
    actor_id: str
    params: dict[str, object]


@dataclass(frozen=True)
class _EventPayloadRefs:
    thread_id: str
    task_id: str
    runtime: dict[str, object]


class OpenCaseTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_open_case_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        resolved = resolve_thread(self.agent, params)
        if isinstance(resolved, ToolExecutionResult):
            return resolved
        thread_id, task_id = resolved
        case = self.agent.collaboration_store.open_case({'thread_id': thread_id, 'task_id': task_id, 'title': str(params.get("title") or "协作 case"), 'summary': str(params.get("summary") or ""), 'priority': str(params.get("priority") or "normal"), 'created_by': actor_agent_id(self.agent, params), 'entities': dict_value(params.get("entities")), 'required_capabilities': string_values(params.get("required_capabilities")), 'metadata': dict_value(params.get("metadata"))})
        payload = _open_case_payload(case, _CasePayloadRefs(thread_id, task_id, params))
        payload.update(collaboration_scope_payload(self.agent, params, explicit_keys=("created_by", "actor_agent_id", "agent_id", "run_id")))
        return ok("open_case", payload)


class RaiseCollaborationEventTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_raise_collaboration_event_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        resolved = resolve_thread(self.agent, params)
        if isinstance(resolved, ToolExecutionResult):
            return resolved
        thread_id, task_id = resolved
        actor_id = actor_agent_id(self.agent, params)
        case = self._open_event_case(_EventCaseInput(thread_id, task_id, actor_id, params))
        request = self._request_event(case.case_id, actor_id, params)
        runtime = target_runtime_summary(self.agent, list(request.target_agent_ids))
        payload = _raise_event_payload(
            case,
            request,
            _EventPayloadRefs(thread_id, task_id, runtime),
        )
        payload.update(collaboration_scope_payload(self.agent, params, explicit_keys=("created_by", "actor_agent_id", "agent_id", "run_id")))
        return ok("raise_collaboration_event", payload)

    def _open_event_case(self, event: _EventCaseInput):
        params = event.params
        return self.agent.collaboration_store.open_case({'thread_id': event.thread_id, 'task_id': event.task_id, 'title': str(params.get("title") or "协作事件"), 'summary': str(params.get("summary") or str(params.get("problem_statement") or "")), 'priority': str(params.get("priority") or "normal"), 'created_by': event.actor_id, 'entities': dict_value(params.get("entities")), 'required_capabilities': string_values(params.get("required_capabilities")), 'metadata': {"created_by_tool": "raise_collaboration_event", **dict_value(params.get("metadata"))}})

    def _request_event(self, case_id: str, actor_id: str, params: dict[str, object]):
        targets = resolved_target_agent_ids(self.agent, params)
        runtime = target_runtime_summary(self.agent, targets)
        return self.agent.collaboration_store.request_collaboration({'case_id': case_id, 'requester_agent_id': str(params.get("requester_agent_id") or actor_id), 'required_capabilities': string_values(params.get("required_capabilities")), 'question': str(params.get("question") or ""), 'entities': dict_value(params.get("entities")), 'problem_statement': str(params.get("problem_statement") or ""), 'observed_facts': dict_values(params.get("observed_facts")), 'query_intent': dict_value(params.get("query_intent")), 'query_hints': dict_values(params.get("query_hints")), 'routing_requirements': dict_value(params.get("routing_requirements")), 'response_contract': dict_value(params.get("response_contract")), 'context_refs': string_values(params.get("context_refs")), 'target_agent_ids': targets, 'deadline_at': deadline_at(self.agent, params), 'priority': str(params.get("priority") or ""), 'metadata': {"created_by_tool": "raise_collaboration_event", **dict_value(params.get("metadata")), **target_runtime_metadata(runtime)}})


class UpdateCaseStatusTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_update_case_status_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        case_id = str(params.get("case_id") or "").strip()
        if not case_id:
            return error("update_case_status", "case_id_required", "case_id is required")
        try:
            case = self.agent.collaboration_store.record_case_status({'case_id': case_id, 'status': str(params.get("status") or ""), 'actor_agent_id': actor_agent_id(self.agent, params), 'summary': str(params.get("summary") or ""), 'decision_type': str(params.get("decision_type") or ""), 'evidence_ids': string_values(params.get("evidence_ids")), 'metadata': dict_value(params.get("metadata"))})
        except (KeyError, ValueError) as exc:
            return error("update_case_status", "case_status_update_failed", str(exc))
        decisions = self.agent.collaboration_store.case_decisions(case_id)
        payload = {"case": case.to_dict(), "decision": decisions[-1].to_dict() if decisions else {}}
        payload.update(collaboration_scope_payload(self.agent, params, explicit_keys=("actor_agent_id", "agent_id", "run_id")))
        return ok("update_case_status", payload)


class CaseStatusTool(BaseTool):
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_case_status_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        case_id = str(params.get("case_id") or "").strip()
        if not case_id:
            return error("case_status", "case_id_required", "case_id is required")
        return ok("case_status", self.agent.collaboration_store.case_status(case_id))


def _open_case_payload(case, refs: _CasePayloadRefs) -> dict[str, object]:
    return {"case_id": case.case_id, "case_ref": f"collaboration://case/{case.case_id}", "thread_id": refs.thread_id, "task_id": refs.task_id, "suggested_next_tool": "request_collaboration", "suggested_tool_call": _suggested_request_call(case, refs.params), "message_zh": "open_case 只是打开协作房间；需要别人回应时再调用 request_collaboration，也可用 raise_collaboration_event 一步完成。"}


def _suggested_request_call(case, params: dict[str, object]) -> dict[str, object]:
    return {"tool": "request_collaboration", "case_id": case.case_id, "requester_agent_id": str(params.get("created_by") or ""), "required_capabilities": list(case.required_capabilities), "question": str(params.get("summary") or case.summary or case.title), "problem_statement": str(params.get("summary") or case.summary or ""), "entities": dict(case.entities)}


def _raise_event_payload(case, request, refs: _EventPayloadRefs) -> dict[str, object]:
    return {"case_id": case.case_id, "request_id": request.request_id, "case_ref": f"collaboration://case/{case.case_id}", "request_ref": f"collaboration://request/{request.request_id}", "thread_id": refs.thread_id, "task_id": refs.task_id, "target_agent_ids": list(request.target_agent_ids), "required_capabilities": list(request.required_capabilities), **target_response_payload(refs.runtime), "next_action": "有 available_target_run_ids 时用 suggested_dispatch_tool_call 唤醒目标代理；有 unavailable_targets 时把不可达写进限制并找父代理换路。"}
