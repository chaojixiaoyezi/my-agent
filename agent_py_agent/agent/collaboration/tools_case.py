# LLM: Case-level collaboration tools expose generic case lifecycle actions.
# 模块用途: 实现 raise_collaboration、inspect_collaboration 和 update_collaboration 的 case 侧逻辑。

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..tools import BaseTool, ToolExecutionResult
from .tool_specs import (
    build_inspect_collaboration_spec,
    build_raise_collaboration_spec,
    build_update_collaboration_spec,
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


class RaiseCollaborationTool(BaseTool):
    # LLM: RaiseCollaborationTool keeps one model action for case creation and request fanout.
    # 类用途: 没有 case_id 时打开协作 case；需要响应时同步写 request；已有 case_id 时继续请求协作。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_raise_collaboration_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        actor_id = actor_agent_id(self.agent, params)
        case_id = str(params.get("case_id") or "").strip()
        if case_id:
            case = None
            thread_id = ""
            task_id = ""
        else:
            resolved = resolve_thread(self.agent, params)
            if isinstance(resolved, ToolExecutionResult):
                return resolved
            thread_id, task_id = resolved
            case = self._open_event_case(_EventCaseInput(thread_id, task_id, actor_id, params))
            case_id = case.case_id
        request = self._request_event(case_id, actor_id, params) if _should_request_collaboration(params) else None
        runtime = target_runtime_summary(self.agent, list(request.target_agent_ids) if request is not None else [])
        payload = _raise_collaboration_payload(case, request, _EventPayloadRefs(thread_id, task_id, runtime))
        payload.update(collaboration_scope_payload(self.agent, params, explicit_keys=("created_by", "requester_agent_id", "actor_agent_id", "agent_id", "run_id")))
        return ok("raise_collaboration", payload)

    def _open_event_case(self, event: _EventCaseInput):
        params = event.params
        return self.agent.collaboration_store.open_case({'thread_id': event.thread_id, 'task_id': event.task_id, 'title': str(params.get("title") or "协作事件"), 'summary': str(params.get("summary") or str(params.get("problem_statement") or "")), 'priority': str(params.get("priority") or "normal"), 'created_by': event.actor_id, 'entities': dict_value(params.get("entities")), 'required_capabilities': string_values(params.get("required_capabilities")), 'metadata': {"created_by_tool": "raise_collaboration", **dict_value(params.get("metadata"))}})

    def _request_event(self, case_id: str, actor_id: str, params: dict[str, object]):
        targets = resolved_target_agent_ids(self.agent, params)
        runtime = target_runtime_summary(self.agent, targets)
        return self.agent.collaboration_store.request_collaboration({'case_id': case_id, 'requester_agent_id': actor_id, 'required_capabilities': string_values(params.get("required_capabilities")), 'question': str(params.get("question") or ""), 'entities': dict_value(params.get("entities")), 'problem_statement': str(params.get("problem_statement") or ""), 'observed_facts': dict_values(params.get("observed_facts")), 'query_intent': dict_value(params.get("query_intent")), 'query_hints': dict_values(params.get("query_hints")), 'routing_requirements': dict_value(params.get("routing_requirements")), 'response_contract': dict_value(params.get("response_contract")), 'context_refs': string_values(params.get("context_refs")), 'target_agent_ids': targets, 'deadline_at': deadline_at(self.agent, params), 'priority': str(params.get("priority") or ""), 'metadata': {"created_by_tool": "raise_collaboration", **dict_value(params.get("metadata")), **target_runtime_metadata(runtime)}})


class UpdateCollaborationTool(BaseTool):
    # LLM: UpdateCollaborationTool merges case lifecycle, request lifecycle, and reroute updates.
    # 类用途: 有 request_id 时更新协作请求；没有 request_id 时更新 case 状态。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_update_collaboration_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        if str(params.get("request_id") or "").strip():
            from .tools_request import update_request

            return update_request(self.agent, "update_collaboration", params, targets=string_values(params.get("target_agent_ids")))
        case_id = str(params.get("case_id") or "").strip()
        if not case_id:
            return error("update_collaboration", "case_id_required", "case_id is required")
        try:
            case = self.agent.collaboration_store.record_case_status({'case_id': case_id, 'status': str(params.get("status") or ""), 'actor_agent_id': actor_agent_id(self.agent, params), 'summary': str(params.get("summary") or ""), 'decision_type': str(params.get("decision_type") or ""), 'evidence_ids': string_values(params.get("evidence_ids")), 'metadata': dict_value(params.get("metadata"))})
        except (KeyError, ValueError) as exc:
            return error("update_collaboration", "case_status_update_failed", str(exc))
        decisions = self.agent.collaboration_store.case_decisions(case_id)
        payload = {"case": case.to_dict(), "decision": decisions[-1].to_dict() if decisions else {}}
        payload.update(collaboration_scope_payload(self.agent, params, explicit_keys=("actor_agent_id", "agent_id", "run_id")))
        return ok("update_collaboration", payload)


class InspectCollaborationTool(BaseTool):
    # LLM: InspectCollaborationTool keeps collaboration reads behind one model-visible tool.
    # 类用途: 有 case_id 时读 case；否则按当前/指定代理列出待处理协作请求。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_inspect_collaboration_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        case_id = str(params.get("case_id") or "").strip()
        if not case_id:
            from .tools_request import list_pending_requests

            return list_pending_requests(self.agent, params)
        return ok("inspect_collaboration", self.agent.collaboration_store.case_status(case_id))


def _should_request_collaboration(params: dict[str, object]) -> bool:
    return any(
        params.get(key) not in (None, "", [], {})
        for key in (
            "question",
            "target_agent_ids",
            "required_capabilities",
            "observed_facts",
            "query_hints",
            "problem_statement",
        )
    )


def _open_case_payload(case, refs: _CasePayloadRefs) -> dict[str, object]:
    return {"case_id": case.case_id, "case_ref": f"collaboration://case/{case.case_id}", "thread_id": refs.thread_id, "task_id": refs.task_id}


def _raise_collaboration_payload(case, request, refs: _EventPayloadRefs) -> dict[str, object]:
    case_id = str(getattr(case, "case_id", "") or getattr(request, "case_id", ""))
    payload: dict[str, object] = {
        "case_id": case_id,
        "case_ref": f"collaboration://case/{case_id}" if case_id else "",
        "thread_id": refs.thread_id,
        "task_id": refs.task_id,
    }
    if request is not None:
        payload.update({
            "request_id": request.request_id,
            "request_ref": f"collaboration://request/{request.request_id}",
            "target_agent_ids": list(request.target_agent_ids),
            "required_capabilities": list(request.required_capabilities),
            **target_response_payload(refs.runtime),
            "next_action": "有 available_target_run_ids 时可 dispatch_subagents 唤醒目标代理；到 deadline 后带已回/未回结果继续推进。",
        })
    return payload
