# LLM: Request ledger operations for targeted collaboration.
# 模块用途: 创建、更新和查询协作请求，支持多目标响应覆盖率。

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from ..io.jsonl import append_jsonl
from .identity import request_update_targets
from .models import CaseDecision, CollaborationRequest, new_decision_id, new_request_id
from .request_status import (
    evidence_sources_by_request,
    is_blocked_request_status,
    is_completed_request_status,
    is_declined_request_status,
    pending_request_row,
    request_has_required_evidence,
)
from .store_cases import CollaborationCaseStore
from .store_common import dict_items, read_jsonl, strings
from .store_common import now as current_time


@dataclass(frozen=True)
class _CoveredStatusRequest:
    request: CollaborationRequest
    targets: tuple[str, ...]
    status_text: str
    metadata: dict[str, Any]
    kwargs: dict[str, Any]


class CollaborationRequestStore(CollaborationCaseStore):
    def request_collaboration(self, request_data: dict) -> CollaborationRequest:
        case_id = str(request_data.get("case_id") or "")
        case = self.load_case(case_id)
        required = strings(request_data.get("required_capabilities"))
        request_params = {"target_agent_ids": request_data.get("target_agent_ids") or [], "requester_agent_id": request_data.get("requester_agent_id", "")}
        targets = self._request_targets(required, request_params)
        current = current_time(request_data.get("now"))
        request = CollaborationRequest(
            request_id=new_request_id(),
            case_id=case.case_id,
            requester_agent_id=str(request_data.get("requester_agent_id") or ""),
            target_agent_ids=targets,
            required_capabilities=required,
            question=str(request_data.get("question") or ""),
            entities=request_data.get("entities") or case.entities,
            problem_statement=str(request_data.get("problem_statement") or ""),
            observed_facts=dict_items(request_data.get("observed_facts")),
            query_intent=request_data.get("query_intent") or {},
            query_hints=dict_items(request_data.get("query_hints")),
            routing_requirements=request_data.get("routing_requirements") or {},
            response_contract=request_data.get("response_contract") or {},
            context_refs=strings(request_data.get("context_refs")),
            priority=str(request_data.get("priority") or case.priority),
            deadline_at=float(request_data.get("deadline_at") or 0.0),
            created_at=current,
            updated_at=current,
            metadata=request_data.get("metadata") or {},
        )
        append_jsonl(self._requests_path(case_id), request.to_dict(), sort_keys=True)
        self._add_request_participants(case_id, request, current)
        return request

    def update_request_status(self, request_data: dict) -> CollaborationRequest:
        case_id = str(request_data.get("case_id") or "")
        self.load_case(case_id)
        request_id = str(request_data.get("request_id") or "")
        request = self._request_by_id(case_id, request_id)
        status_text = str(request_data.get("status") or "").strip()
        if not status_text:
            raise ValueError("status_required")
        kwargs = {
            "summary": request_data.get("summary", ""),
            "actor_agent_id": request_data.get("actor_agent_id", ""),
            "target_agent_ids": request_data.get("target_agent_ids") or [],
            "now": request_data.get("now"),
            "metadata": request_data.get("metadata") or {},
        }
        updated = self._updated_request_snapshot(request, status_text, kwargs)
        append_jsonl(self._requests_path(case_id), updated.to_dict(), sort_keys=True)
        self._add_rerouted_participants(case_id, request, updated, kwargs)
        self._append_request_status_decision(case_id, updated, kwargs)
        return updated

    def case_requests(self, case_id: str) -> list[CollaborationRequest]:
        requests: dict[str, CollaborationRequest] = {}
        for row in read_jsonl(self._requests_path(case_id)):
            request = CollaborationRequest.from_dict(row)
            if request.request_id:
                requests[request.request_id] = request
        return list(requests.values())

    def case_request_history(self, case_id: str) -> list[CollaborationRequest]:
        return [CollaborationRequest.from_dict(row) for row in read_jsonl(self._requests_path(case_id))]

    def pending_requests_for_agent(self, *, agent_id: str, agent_name: str = "", agent_role: str = "", limit: int = 10) -> list[dict[str, Any]]:
        identities = self.agent_identity_aliases((agent_id, agent_name, agent_role))
        rows = self._pending_request_rows_for_identities(identities) if identities else []
        rows.sort(key=lambda item: (str(item.get("priority") or ""), float(item.get("created_at") or 0.0)))
        return rows if limit <= 0 else rows[:limit]

    def _request_targets(self, required: tuple[str, ...], kwargs: dict[str, Any]) -> tuple[str, ...]:
        targets = strings(kwargs.get("target_agent_ids"))
        if targets:
            return targets
        return tuple(
            item.agent_id
            for item in self.match_agents(
                required_capabilities=required,
                exclude_agent_id=str(kwargs.get("requester_agent_id") or ""),
                limit=20,
            )
        )

    def _add_request_participants(self, case_id: str, request: CollaborationRequest, current: float) -> None:
        for agent_id in request.target_agent_ids:
            self.add_participant({"case_id": case_id, "agent_id": agent_id, "role": "responder", "status": "requested", "requested_capabilities": request.required_capabilities, "now": current})

    def _request_by_id(self, case_id: str, request_id: str) -> CollaborationRequest:
        request_id_text = str(request_id or "").strip()
        if not request_id_text:
            raise ValueError("request_id_required")
        requests = {item.request_id: item for item in self.case_requests(case_id)}
        request = requests.get(request_id_text)
        if request is None:
            raise KeyError(f"unknown collaboration request: {request_id_text}")
        return request

    def _updated_request_snapshot(self, request: CollaborationRequest, status_text: str, kwargs: dict[str, Any]) -> CollaborationRequest:
        current = current_time(kwargs.get("now"))
        metadata = self._next_request_metadata(request, status_text, kwargs)
        targets = request_update_targets(explicit_targets=kwargs.get("target_agent_ids"), metadata=kwargs.get("metadata") or {})
        status = self._covered_status(_CoveredStatusRequest(request, targets, status_text, metadata, kwargs))
        return replace(request, target_agent_ids=targets or request.target_agent_ids, status=status, updated_at=current, metadata=metadata)

    def _next_request_metadata(self, request: CollaborationRequest, status_text: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        metadata = {**request.metadata, **(kwargs.get("metadata") or {})}
        summary = str(kwargs.get("summary") or "").strip()
        if kwargs.get("actor_agent_id"):
            metadata["last_actor_agent_id"] = str(kwargs.get("actor_agent_id") or "")
        if summary:
            metadata["status_summary"] = summary
        targets = request_update_targets(explicit_targets=kwargs.get("target_agent_ids"), metadata=kwargs.get("metadata") or {})
        if targets and tuple(request.target_agent_ids) != targets:
            metadata.setdefault("original_target_agent_ids", list(request.target_agent_ids))
            metadata.update({"rerouted_from": list(request.target_agent_ids), "rerouted_to": list(targets)})
        return metadata

    def _covered_status(self, request_data: _CoveredStatusRequest) -> str:
        request = request_data.request
        status_text = request_data.status_text
        if not (is_completed_request_status(status_text) and len(request.target_agent_ids) > 1):
            return status_text
        sources = evidence_sources_by_request(self.case_evidence(request.case_id), aliases=self.agent_identity_aliases).get(request.request_id, set())
        if request_has_required_evidence(request, sources, target_aliases=self.agent_identity_aliases):
            return status_text
        request_data.metadata["partial_completion_by"] = str(request_data.kwargs.get("actor_agent_id") or "")
        request_data.metadata["partial_completion_summary"] = str(request_data.kwargs.get("summary") or "")
        return "pending"

    def _add_rerouted_participants(self, case_id: str, old: CollaborationRequest, updated: CollaborationRequest, kwargs: dict[str, Any]) -> None:
        for agent_id in updated.target_agent_ids:
            if agent_id not in old.target_agent_ids:
                self.add_participant({"case_id": case_id, "agent_id": agent_id, "role": "responder", "status": "requested", "requested_capabilities": old.required_capabilities, "now": kwargs.get("now"), "metadata": {"request_id": old.request_id, "rerouted": True}})

    def _append_request_status_decision(self, case_id: str, request: CollaborationRequest, kwargs: dict[str, Any]) -> None:
        summary = str(kwargs.get("summary") or "").strip() or f"request {request.request_id} status={request.status}"
        self.append_decision(CaseDecision(decision_id=new_decision_id(), case_id=case_id, decision_type="collaboration_request_status_update", summary=summary, created_at=current_time(kwargs.get("now")), metadata={"request_id": request.request_id, "request_status": request.status, "actor_agent_id": str(kwargs.get("actor_agent_id") or ""), **(kwargs.get("metadata") or {})}))

    def _pending_request_rows_for_identities(self, identities: set[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for case in self.list_cases(status="open"):
            rows.extend(self._pending_rows_for_case(case, identities))
        return rows

    def _pending_rows_for_case(self, case, identities: set[str]) -> list[dict[str, Any]]:
        evidence_sources = evidence_sources_by_request(self.case_evidence(case.case_id), aliases=self.agent_identity_aliases)
        return [
            pending_request_row(case, request)
            for request in self.case_requests(case.case_id)
            if self._request_waits_for_identity(request, identities, evidence_sources)
        ]

    def _request_waits_for_identity(self, request: CollaborationRequest, identities: set[str], evidence_sources: dict[str, set[str]]) -> bool:
        target_aliases = self.agent_identity_aliases(request.target_agent_ids)
        if not identities.intersection(target_aliases):
            return False
        if identities.intersection(evidence_sources.get(request.request_id, set())):
            return False
        return not (is_declined_request_status(request.status) or is_blocked_request_status(request.status))
