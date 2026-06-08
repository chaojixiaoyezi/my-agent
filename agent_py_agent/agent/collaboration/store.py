"""Collaboration store for cases, requests, evidence, participants,
decisions, and agent capabilities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..gateway_parts.io import read_json_file, update_json_file_atomic, write_json_file_atomic
from ..io.jsonl import append_jsonl
from ..runtime_errors import runtime_error_report
from .coverage import case_response_coverage
from .identity import agent_identity_keys, request_update_targets
from .models import (
    AGENT_CAPABILITY_STATUS_AVAILABLE,
    AgentCapability,
    CaseDecision,
    CaseParticipant,
    CollaborationCase,
    CollaborationRequest,
    EvidencePacket,
    new_case_id,
    new_decision_id,
    new_evidence_id,
    new_request_id,
)
from .request_status import (
    CollaborationRequestStatus,
    canonical_case_status_for_update,
    canonical_request_status_for_update,
    case_overview_row,
    case_status_protocol_metadata,
    case_status_text,
    case_window_status,
    evidence_sources_by_request,
    is_blocked_request_status,
    is_completed_request_status,
    is_declined_request_status,
    is_terminal_status,
    missing_responder_agent_ids_by_request,
    pending_request_row,
    request_has_required_evidence,
    request_is_effectively_timed_out,
    request_response_status,
    request_status_protocol_metadata,
    unavailable_target_agent_ids_by_request,
)
from .store_common import dict_items, now, read_jsonl_report, strings
from .store_common import now as current_time

# ---------------------------------------------------------------------------
# shared layout
# ---------------------------------------------------------------------------

class CollaborationBaseStore:
    """Shared directory layout for case/request/evidence ledgers."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.cases_dir = self.root / "cases"
        self.requests_dir = self.root / "requests"
        self.evidence_dir = self.root / "evidence"
        self.participants_dir = self.root / "participants"
        self.decisions_dir = self.root / "decisions"
        self.capabilities_path = self.root / "agent_capabilities.json"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for path in (
            self.cases_dir,
            self.requests_dir,
            self.evidence_dir,
            self.participants_dir,
            self.decisions_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _write_case(self, case: CollaborationCase) -> None:
        write_json_file_atomic(self._case_path(case.case_id), case.to_dict())

    def _case_path(self, case_id: str) -> Path:
        return self.cases_dir / f"{case_id}.json"

    def _requests_path(self, case_id: str) -> Path:
        return self.requests_dir / f"{case_id}.jsonl"

    def _evidence_path(self, case_id: str) -> Path:
        return self.evidence_dir / f"{case_id}.jsonl"

    def _participants_path(self, case_id: str) -> Path:
        return self.participants_dir / f"{case_id}.jsonl"

    def _decisions_path(self, case_id: str) -> Path:
        return self.decisions_dir / f"{case_id}.jsonl"


# ---------------------------------------------------------------------------
# evidence records
# ---------------------------------------------------------------------------

class CollaborationEvidenceStore(CollaborationBaseStore):
    def add_participant(self, request: dict) -> CaseParticipant:
        participant = CaseParticipant(
            case_id=str(request.get("case_id") or ""),
            agent_id=str(request.get("agent_id") or ""),
            role=str(request.get("role") or ""),
            status=str(request.get("status") or "requested"),
            requested_capabilities=strings(request.get("requested_capabilities")),
            joined_at=current_time(request.get("now")),
            metadata=request.get("metadata") or {},
        )
        append_jsonl(self._participants_path(participant.case_id), participant.to_dict(), sort_keys=True)
        return participant

    def submit_evidence(self, request: dict) -> EvidencePacket:
        case_id = str(request.get("case_id") or "")
        self.load_case(case_id)
        evidence = EvidencePacket(
            evidence_id=new_evidence_id(),
            case_id=case_id,
            request_id=str(request.get("request_id") or ""),
            source_agent_id=str(request.get("source_agent_id") or ""),
            matched=bool(request.get("matched")),
            summary=str(request.get("summary") or ""),
            evidence_refs=strings(request.get("evidence_refs")),
            queried_scopes=strings(request.get("queried_scopes")),
            used_query_hints=strings(request.get("used_query_hints")),
            miss_reason=str(request.get("miss_reason") or ""),
            response_facts=dict_items(request.get("response_facts")),
            followup_suggestions=dict_items(request.get("followup_suggestions")),
            query_actions=dict_items(request.get("query_actions")),
            confidence=float(request.get("confidence") or 0.0),
            limitations=strings(request.get("limitations")),
            created_at=current_time(request.get("now")),
            metadata=request.get("metadata") or {},
        )
        append_jsonl(self._evidence_path(case_id), evidence.to_dict(), sort_keys=True)
        return evidence

    def case_evidence(self, case_id: str) -> list[EvidencePacket]:
        evidence, _load_errors = self.case_evidence_report(case_id)
        return evidence

    def case_evidence_report(self, case_id: str) -> tuple[list[EvidencePacket], list[dict[str, Any]]]:
        report = read_jsonl_report(self._evidence_path(case_id), context="collaboration.evidence.read")
        return [EvidencePacket.from_dict(row) for row in report.rows], report.load_errors

    def case_participants(self, case_id: str) -> list[CaseParticipant]:
        participants, _load_errors = self.case_participants_report(case_id)
        return participants

    def case_participants_report(self, case_id: str) -> tuple[list[CaseParticipant], list[dict[str, Any]]]:
        report = read_jsonl_report(self._participants_path(case_id), context="collaboration.participants.read")
        return [CaseParticipant.from_dict(row) for row in report.rows], report.load_errors

    def append_decision(self, decision: CaseDecision) -> CaseDecision:
        append_jsonl(self._decisions_path(decision.case_id), decision.to_dict(), sort_keys=True)
        return decision

    def case_decisions(self, case_id: str) -> list[CaseDecision]:
        decisions, _load_errors = self.case_decisions_report(case_id)
        return decisions

    def case_decisions_report(self, case_id: str) -> tuple[list[CaseDecision], list[dict[str, Any]]]:
        report = read_jsonl_report(self._decisions_path(case_id), context="collaboration.decisions.read")
        return [CaseDecision.from_dict(row) for row in report.rows], report.load_errors


# ---------------------------------------------------------------------------
# agent capabilities
# ---------------------------------------------------------------------------

def _capability_roster_load_error(path: Path, exc: BaseException) -> dict[str, Any]:
    report = runtime_error_report(exc, context="collaboration.agent_capabilities.read")
    report["path"] = str(path)
    return report


class CollaborationCapabilityStore(CollaborationEvidenceStore):
    def register_agent(self, capability: AgentCapability) -> AgentCapability:
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            return {**data, capability.agent_id: capability.to_dict()}

        update_json_file_atomic(self.capabilities_path, updater)
        return capability

    def agent_capabilities(self) -> list[AgentCapability]:
        capabilities, _load_error = self.agent_capabilities_report()
        return capabilities

    def agent_capabilities_report(self) -> tuple[list[AgentCapability], dict[str, Any] | None]:
        if not self.capabilities_path.exists():
            return [], None
        try:
            data = json.loads(self.capabilities_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"agent capabilities roster is {type(data).__name__}, expected object")
        except (OSError, UnicodeError, ValueError) as exc:
            return [], _capability_roster_load_error(self.capabilities_path, exc)
        return [AgentCapability.from_dict(item) for item in data.values() if isinstance(item, dict)], None

    def agent_identity_keys(self, values: object = ()) -> set[str]:
        return agent_identity_keys(values)

    def match_agents(
        self,
        *,
        required_capabilities: list[str] | tuple[str, ...] | None = None,
        exclude_agent_id: str = "",
        limit: int = 20,
    ) -> list[AgentCapability]:
        matches, _load_error = self.match_agents_report(
            required_capabilities=required_capabilities,
            exclude_agent_id=exclude_agent_id,
            limit=limit,
        )
        return matches

    def match_agents_report(
        self,
        *,
        required_capabilities: list[str] | tuple[str, ...] | None = None,
        exclude_agent_id: str = "",
        limit: int = 20,
    ) -> tuple[list[AgentCapability], dict[str, Any] | None]:
        required = {str(item) for item in (required_capabilities or []) if str(item or "").strip()}
        capabilities, load_error = self.agent_capabilities_report()
        matches = [item for item in capabilities if self._matches(item, required, exclude_agent_id)]
        matches.sort(key=lambda item: (item.load, item.agent_id))
        matches = matches if limit <= 0 else matches[:limit]
        return matches, load_error

    def _matches(self, capability: AgentCapability, required: set[str], exclude_agent_id: str) -> bool:
        if capability.agent_id == exclude_agent_id:
            return False
        if capability.status != AGENT_CAPABILITY_STATUS_AVAILABLE:
            return False
        return not required or required.issubset(set(capability.capabilities))


# ---------------------------------------------------------------------------
# collaboration cases
# ---------------------------------------------------------------------------

class CollaborationCaseStore(CollaborationCapabilityStore):
    def open_case(self, request: dict) -> CollaborationCase:
        current = current_time(request.get("now"))
        case = CollaborationCase(
            case_id=new_case_id(),
            thread_id=str(request.get("thread_id") or ""),
            task_id=str(request.get("task_id") or ""),
            title=str(request.get("title") or ""),
            summary=str(request.get("summary") or ""),
            priority=str(request.get("priority") or "normal"),
            created_by=str(request.get("created_by") or ""),
            required_capabilities=strings(request.get("required_capabilities")),
            entities=request.get("entities") or {},
            created_at=current,
            updated_at=current,
            metadata=request.get("metadata") or {},
        )
        self._write_case(case)
        if case.created_by:
            self.add_participant({
                "case_id": case.case_id,
                "agent_id": case.created_by,
                "role": "creator",
                "status": "joined",
                "now": current,
            })
        return case

    def load_case(self, case_id: str) -> CollaborationCase:
        data = read_json_file(self._case_path(case_id))
        if not data:
            raise KeyError(f"unknown collaboration case: {case_id}")
        return CollaborationCase.from_dict(data)

    def list_cases(self, *, status: str | None = None) -> list[CollaborationCase]:
        cases = [case for case in self._all_cases() if status is None or case.status == status]
        cases.sort(key=lambda item: item.created_at)
        return cases

    def update_case_status(
        self,
        case_id: str,
        *,
        status: str,
        now: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CollaborationCase:
        case = self.load_case(case_id)
        next_metadata = dict(case.metadata)
        next_metadata.pop("raw_case_status", None)
        next_metadata.pop("case_status_protocol_error", None)
        if metadata:
            next_metadata.update(metadata)
        raw_status = str(status or case.status)
        next_metadata.update(case_status_protocol_metadata(raw_status))
        updated = replace(
            case,
            status=canonical_case_status_for_update(raw_status),
            updated_at=now if now is not None else __import__("time").time(),
            metadata=next_metadata,
        )
        self._write_case(updated)
        return updated

    def record_case_status(self, request: dict) -> CollaborationCase:
        case_id = str(request.get("case_id") or "")
        status_text = str(request.get("status") or "").strip()
        if not status_text:
            raise ValueError("status_required")
        kwargs = {
            "summary": request.get("summary", ""),
            "decision_type": request.get("decision_type", ""),
            "evidence_ids": request.get("evidence_ids") or [],
            "actor_agent_id": request.get("actor_agent_id", ""),
            "metadata": request.get("metadata") or {},
            "now": request.get("now"),
        }
        canonical_status = canonical_case_status_for_update(status_text)
        protocol_metadata = case_status_protocol_metadata(status_text)
        kwargs["metadata"] = {**kwargs["metadata"], **protocol_metadata}
        self._validate_terminal_case_status(case_id, canonical_status, kwargs)
        updated = self.update_case_status(
            case_id, status=canonical_status, now=kwargs.get("now"), metadata=protocol_metadata,
        )
        self._append_status_decision(case_id, canonical_status, kwargs)
        return updated

    def _all_cases(self) -> list[CollaborationCase]:
        rows: list[CollaborationCase] = []
        for path in sorted(self.cases_dir.glob("*.json")):
            data = read_json_file(path)
            if data:
                rows.append(CollaborationCase.from_dict(data))
        return rows

    def _validate_terminal_case_status(self, case_id: str, status_text: str, kwargs: dict[str, Any]) -> None:
        summary = str(kwargs.get("summary") or "").strip()
        decision_type = str(kwargs.get("decision_type") or "").strip()
        if is_terminal_status(status_text) and not (summary or decision_type or self.case_decisions(case_id)):
            raise ValueError("summary_or_decision_required")

    def _append_status_decision(self, case_id: str, status_text: str, kwargs: dict[str, Any]) -> None:
        summary = str(kwargs.get("summary") or "").strip()
        decision_type = str(kwargs.get("decision_type") or "").strip()
        if not (summary or decision_type):
            return
        self.append_decision(
            CaseDecision(
                decision_id=new_decision_id(),
                case_id=case_id,
                decision_type=decision_type or "case_status_update",
                summary=summary,
                evidence_ids=strings(kwargs.get("evidence_ids")),
                created_at=current_time(kwargs.get("now")),
                metadata={
                    "actor_agent_id": str(kwargs.get("actor_agent_id") or ""),
                    "status": status_text,
                    **(kwargs.get("metadata") or {}),
                },
            )
        )


# ---------------------------------------------------------------------------
# collaboration requests
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _CoveredStatusRequest:
    request: CollaborationRequest
    targets: tuple[str, ...]
    status_text: str
    metadata: dict[str, Any]
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class _RequestTargetResolution:
    targets: tuple[str, ...]
    load_error: dict[str, Any] | None = None


class CollaborationRequestStore(CollaborationCaseStore):
    def request_collaboration(self, request_data: dict) -> CollaborationRequest:
        case_id = str(request_data.get("case_id") or "")
        case = self.load_case(case_id)
        required = strings(request_data.get("required_capabilities"))
        request_params = {
            "target_agent_ids": request_data.get("target_agent_ids") or [],
            "requester_agent_id": request_data.get("requester_agent_id", ""),
        }
        target_resolution = self._request_targets_report(required, request_params)
        current = current_time(request_data.get("now"))
        metadata = dict(request_data.get("metadata") or {})
        if target_resolution.load_error:
            metadata["capability_roster_load_error"] = target_resolution.load_error
        request = CollaborationRequest(
            request_id=new_request_id(),
            case_id=case.case_id,
            requester_agent_id=str(request_data.get("requester_agent_id") or ""),
            target_agent_ids=target_resolution.targets,
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
            metadata=metadata,
        )
        append_jsonl(self._requests_path(case_id), request.to_dict(), sort_keys=True)
        self._add_request_participants(case_id, request, current)
        return request

    def case_requests(self, case_id: str) -> list[CollaborationRequest]:
        requests, _load_errors = self.case_requests_report(case_id)
        return requests

    def case_requests_report(self, case_id: str) -> tuple[list[CollaborationRequest], list[dict[str, Any]]]:
        requests: dict[str, CollaborationRequest] = {}
        report = read_jsonl_report(self._requests_path(case_id), context="collaboration.requests.read")
        for row in report.rows:
            request = CollaborationRequest.from_dict(row)
            if request.request_id:
                requests[request.request_id] = request
        return list(requests.values()), report.load_errors

    def case_request_history(self, case_id: str) -> list[CollaborationRequest]:
        requests, _load_errors = self.case_request_history_report(case_id)
        return requests

    def case_request_history_report(self, case_id: str) -> tuple[list[CollaborationRequest], list[dict[str, Any]]]:
        report = read_jsonl_report(self._requests_path(case_id), context="collaboration.requests.read")
        return [CollaborationRequest.from_dict(row) for row in report.rows], report.load_errors

    def _request_targets(self, required: tuple[str, ...], kwargs: dict[str, Any]) -> tuple[str, ...]:
        return self._request_targets_report(required, kwargs).targets

    def _request_targets_report(
        self, required: tuple[str, ...], kwargs: dict[str, Any]
    ) -> _RequestTargetResolution:
        targets = strings(kwargs.get("target_agent_ids"))
        if targets:
            return _RequestTargetResolution(targets)
        matches, load_error = self.match_agents_report(
            required_capabilities=required,
            exclude_agent_id=str(kwargs.get("requester_agent_id") or ""),
            limit=20,
        )
        return _RequestTargetResolution(
            tuple(item.agent_id for item in matches),
            load_error,
        )

    def _add_request_participants(self, case_id: str, request: CollaborationRequest, current: float) -> None:
        for agent_id in request.target_agent_ids:
            self.add_participant({
                "case_id": case_id,
                "agent_id": agent_id,
                "role": "responder",
                "status": "requested",
                "requested_capabilities": request.required_capabilities,
                "now": current,
            })


class CollaborationRequestStatusStore(CollaborationRequestStore):
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

    def _request_by_id(self, case_id: str, request_id: str) -> CollaborationRequest:
        request_id_text = str(request_id or "").strip()
        if not request_id_text:
            raise ValueError("request_id_required")
        requests = {item.request_id: item for item in self.case_requests(case_id)}
        request = requests.get(request_id_text)
        if request is None:
            raise KeyError(f"unknown collaboration request: {request_id_text}")
        return request

    def _updated_request_snapshot(
        self, request: CollaborationRequest, status_text: str, kwargs: dict[str, Any]
    ) -> CollaborationRequest:
        current = current_time(kwargs.get("now"))
        metadata = self._next_request_metadata(request, status_text, kwargs)
        canonical_status = canonical_request_status_for_update(status_text)
        targets = request_update_targets(
            explicit_targets=kwargs.get("target_agent_ids"),
            metadata=kwargs.get("metadata") or {},
        )
        status = self._covered_status(
            _CoveredStatusRequest(request, targets, canonical_status, metadata, kwargs)
        )
        return replace(
            request,
            target_agent_ids=targets or request.target_agent_ids,
            status=status,
            updated_at=current,
            metadata=metadata,
        )

    def _next_request_metadata(
        self, request: CollaborationRequest, status_text: str, kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        metadata = {**request.metadata, **(kwargs.get("metadata") or {})}
        metadata.pop("raw_request_status", None)
        metadata.pop("request_status_protocol_error", None)
        metadata.update(request_status_protocol_metadata(status_text))
        summary = str(kwargs.get("summary") or "").strip()
        if kwargs.get("actor_agent_id"):
            metadata["last_actor_agent_id"] = str(kwargs.get("actor_agent_id") or "")
        if summary:
            metadata["status_summary"] = summary
        targets = request_update_targets(
            explicit_targets=kwargs.get("target_agent_ids"),
            metadata=kwargs.get("metadata") or {},
        )
        if targets and tuple(request.target_agent_ids) != targets:
            metadata.setdefault("original_target_agent_ids", list(request.target_agent_ids))
            metadata.update({"rerouted_from": list(request.target_agent_ids), "rerouted_to": list(targets)})
        return metadata

    def _covered_status(self, request_data: _CoveredStatusRequest) -> str:
        request = request_data.request
        status_text = request_data.status_text
        if not (is_completed_request_status(status_text) and len(request.target_agent_ids) > 1):
            return status_text
        sources = evidence_sources_by_request(
            self.case_evidence(request.case_id),
            identity_keys=self.agent_identity_keys,
        ).get(request.request_id, set())
        if request_has_required_evidence(request, sources, target_identity_keys=self.agent_identity_keys):
            return status_text
        request_data.metadata["partial_completion_by"] = str(
            request_data.kwargs.get("actor_agent_id") or ""
        )
        request_data.metadata["partial_completion_summary"] = str(
            request_data.kwargs.get("summary") or ""
        )
        return CollaborationRequestStatus.PENDING.value

    def _add_rerouted_participants(
        self, case_id: str, old: CollaborationRequest, updated: CollaborationRequest, kwargs: dict[str, Any]
    ) -> None:
        for agent_id in updated.target_agent_ids:
            if agent_id not in old.target_agent_ids:
                self.add_participant({
                    "case_id": case_id,
                    "agent_id": agent_id,
                    "role": "responder",
                    "status": "requested",
                    "requested_capabilities": old.required_capabilities,
                    "now": kwargs.get("now"),
                    "metadata": {"request_id": old.request_id, "rerouted": True},
                })

    def _append_request_status_decision(
        self, case_id: str, request: CollaborationRequest, kwargs: dict[str, Any]
    ) -> None:
        summary = (
            str(kwargs.get("summary") or "").strip()
            or f"request {request.request_id} status={request.status}"
        )
        self.append_decision(
            CaseDecision(
                decision_id=new_decision_id(),
                case_id=case_id,
                decision_type="collaboration_request_status_update",
                summary=summary,
                created_at=current_time(kwargs.get("now")),
                metadata={
                    "request_id": request.request_id,
                    "request_status": request.status,
                    "actor_agent_id": str(kwargs.get("actor_agent_id") or ""),
                    **(kwargs.get("metadata") or {}),
                },
            )
        )


class CollaborationPendingRequestStore(CollaborationRequestStatusStore):
    def pending_requests_for_agent(
        self, *, agent_id: str, agent_name: str = "", agent_role: str = "", limit: int = 10
    ) -> list[dict[str, Any]]:
        rows, _load_errors = self.pending_requests_for_agent_report(
            agent_id=agent_id, agent_name=agent_name, agent_role=agent_role, limit=limit,
        )
        return rows

    def pending_requests_for_agent_report(
        self, *, agent_id: str, agent_name: str = "", agent_role: str = "", limit: int = 10
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        identities = self.agent_identity_keys((agent_id, agent_name, agent_role))
        rows, load_errors = (
            self._pending_request_rows_for_identities_report(identities) if identities else ([], [])
        )
        rows.sort(key=lambda item: (str(item.get("priority") or ""), float(item.get("created_at") or 0.0)))
        return (rows if limit <= 0 else rows[:limit]), load_errors

    def _pending_request_rows_for_identities(self, identities: set[str]) -> list[dict[str, Any]]:
        rows, _load_errors = self._pending_request_rows_for_identities_report(identities)
        return rows

    def _pending_request_rows_for_identities_report(
        self, identities: set[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        load_errors: list[dict[str, Any]] = []
        for case in self.list_cases(status="open"):
            case_rows, case_errors = self._pending_rows_for_case_report(case, identities)
            rows.extend(case_rows)
            load_errors.extend(case_errors)
        return rows, load_errors

    def _pending_rows_for_case(self, case, identities: set[str]) -> list[dict[str, Any]]:
        rows, _load_errors = self._pending_rows_for_case_report(case, identities)
        return rows

    def _pending_rows_for_case_report(
        self, case, identities: set[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        evidence, evidence_errors = self.case_evidence_report(case.case_id)
        requests_results, request_errors = self.case_requests_report(case.case_id)
        evidence_sources = evidence_sources_by_request(evidence, identity_keys=self.agent_identity_keys)
        return [
            pending_request_row(case, request)
            for request in requests_results
            if self._request_waits_for_identity(request, identities, evidence_sources)
        ], [*evidence_errors, *request_errors]

    def _request_waits_for_identity(
        self, request: CollaborationRequest, identities: set[str], evidence_sources: dict[str, set[str]]
    ) -> bool:
        target_identity_keys = self.agent_identity_keys(request.target_agent_ids)
        if not identities.intersection(target_identity_keys):
            return False
        if identities.intersection(evidence_sources.get(request.request_id, set())):
            return False
        return not (is_declined_request_status(request.status) or is_blocked_request_status(request.status))


# ---------------------------------------------------------------------------
# public collaboration store
# ---------------------------------------------------------------------------

class _CaseSnapshot:
    def __init__(self, store: CollaborationStore, case_id: str):
        self.case = store.load_case(case_id)
        self.requests, request_errors = store.case_requests_report(case_id)
        request_history, _request_history_errors = store.case_request_history_report(case_id)
        self.request_history_count = len(request_history)
        self.evidence, evidence_errors = store.case_evidence_report(case_id)
        self.participants, participant_errors = store.case_participants_report(case_id)
        self.decisions, decision_errors = store.case_decisions_report(case_id)
        self.load_errors = [
            *request_errors,
            *evidence_errors,
            *participant_errors,
            *decision_errors,
        ]
        self.current = now()
        self.identity_keys = store.agent_identity_keys
        self.evidence_sources_by_request = evidence_sources_by_request(
            self.evidence, identity_keys=self.identity_keys,
        )
        self.has_required_evidence = self._required_evidence_index()

    @classmethod
    def from_store(cls, store: CollaborationStore, case_id: str) -> _CaseSnapshot:
        return cls(store, case_id)

    def _required_evidence_index(self) -> dict[str, bool]:
        return {
            item.request_id: request_has_required_evidence(
                item,
                self.evidence_sources_by_request.get(item.request_id, set()),
                target_identity_keys=self.identity_keys,
            )
            for item in self.requests
            if item.request_id
        }


@dataclass(frozen=True)
class _CollectionResultInput:
    snapshot: _CaseSnapshot
    groups: dict[str, list[Any]]
    coverage: dict[str, Any]
    missing: dict[str, list[str]]
    unavailable: dict[str, list[str]]
    ready: bool


def _request_groups(snapshot: _CaseSnapshot) -> dict[str, list[Any]]:
    return {
        "pending": [item for item in snapshot.requests if _is_pending(item, snapshot)],
        "blocked": [item for item in snapshot.requests if is_blocked_request_status(item.status)],
        "timed_out": [item for item in snapshot.requests if _is_timed_out(item, snapshot)],
        "completed": [item for item in snapshot.requests if _is_completed(item, snapshot)],
        "declined": [item for item in snapshot.requests if is_declined_request_status(item.status)],
    }


def _status_payload(snapshot: _CaseSnapshot, groups: dict[str, list[Any]]) -> dict[str, Any]:
    ready = bool(
        groups["blocked"]
        or groups["timed_out"]
        or (not groups["pending"] and (groups["completed"] or snapshot.evidence))
    )
    coverage = case_response_coverage(
        snapshot.requests,
        snapshot.evidence_sources_by_request,
        target_identity_keys=snapshot.identity_keys,
    )
    missing = missing_responder_agent_ids_by_request(
        snapshot.requests,
        snapshot.evidence_sources_by_request,
        target_identity_keys=snapshot.identity_keys,
    )
    unavailable = unavailable_target_agent_ids_by_request(snapshot.requests)
    return {
        "case": snapshot.case.to_dict(),
        "case_window": _case_window(snapshot, groups, ready),
        "collection_result": _collection_result(
            _CollectionResultInput(snapshot, groups, coverage, missing, unavailable, ready)
        ),
        "request_count": len(snapshot.requests),
        "request_history_count": snapshot.request_history_count,
        "evidence_count": len(snapshot.evidence),
        "participant_count": len(snapshot.participants),
        "decision_count": len(snapshot.decisions),
        "pending_request_count": len(groups["pending"]),
        "blocked_request_count": len(groups["blocked"]),
        "timed_out_request_count": len(groups["timed_out"]),
        "completed_request_count": len(groups["completed"]),
        "declined_request_count": len(groups["declined"]),
        "missing_evidence_request_ids": _missing_evidence_request_ids(snapshot),
        "timed_out_request_ids": [item.request_id for item in groups["timed_out"] if item.request_id],
        "missing_responder_agent_ids_by_request": missing,
        "unavailable_target_agent_ids_by_request": unavailable,
        "response_coverage": coverage,
        "ready_for_main_agent": ready,
        "requires_main_agent": ready,
        "load_errors": snapshot.load_errors,
        **_bounded_case_rows(snapshot, groups),
    }


def _case_window(
    snapshot: _CaseSnapshot, groups: dict[str, list[Any]], ready: bool
) -> dict[str, Any]:
    window_status = case_window_status(snapshot.case.status)
    next_action = (
        "上报上级并带上已回/未回/不可达清单。" if ready
        else "继续等待响应，或按需要补发协作请求。"
    )
    return {
        "status": window_status,
        "is_open": window_status == "open",
        "ready_to_report": ready,
        "pending_request_count": len(groups["pending"]),
        "next_action_zh": next_action,
    }


def _collection_result(request: _CollectionResultInput) -> dict[str, Any]:
    return {
        "status": "ready_to_report" if request.ready else "collecting",
        "responded_target_count": int(request.coverage.get("responded_target_count") or 0),
        "missing_target_count": int(request.coverage.get("missing_target_count") or 0),
        "unavailable_target_count": int(request.coverage.get("unavailable_target_count") or 0),
        "evidence_count": len(request.snapshot.evidence),
        "missing_responder_agent_ids_by_request": request.missing,
        "unavailable_target_agent_ids_by_request": request.unavailable,
        "deadline_expired_request_ids": [
            item.request_id for item in request.groups["timed_out"] if item.request_id
        ],
        "next_action_zh": (
            "带部分结果继续推进；不要等所有响应者都回复。"
            if request.ready
            else "到 deadline 再收回；响应者缺席只记为未回复，不阻塞主流程。"
        ),
    }


def _bounded_case_rows(snapshot: _CaseSnapshot, groups: dict[str, list[Any]]) -> dict[str, Any]:
    return {
        "requests": [_request_row(snapshot, item) for item in snapshot.requests[-10:]],
        "pending_requests": [_request_row(snapshot, item) for item in groups["pending"][-10:]],
        "blocked_requests": [_request_row(snapshot, item) for item in groups["blocked"][-10:]],
        "timed_out_requests": [_request_row(snapshot, item) for item in groups["timed_out"][-10:]],
        "completed_requests": [_request_row(snapshot, item) for item in groups["completed"][-10:]],
        "declined_requests": [_request_row(snapshot, item) for item in groups["declined"][-10:]],
        "evidence": [item.to_dict() for item in snapshot.evidence[-10:]],
        "participants": [item.to_dict() for item in snapshot.participants[-20:]],
        "decisions": [item.to_dict() for item in snapshot.decisions[-10:]],
    }


def _request_row(snapshot: _CaseSnapshot, item: Any) -> dict[str, Any]:
    row = item.to_dict()
    row["response_status"] = request_response_status(
        item,
        now=snapshot.current,
        has_required_evidence=snapshot.has_required_evidence.get(item.request_id, False),
    )
    row["has_response"] = row["response_status"] == "responded"
    row["deadline_expired"] = _is_timed_out(item, snapshot)
    return row


def _is_pending(item: Any, snapshot: _CaseSnapshot) -> bool:
    return not (
        is_blocked_request_status(item.status)
        or _is_timed_out(item, snapshot)
        or is_declined_request_status(item.status)
        or snapshot.has_required_evidence.get(item.request_id, False)
    )


def _is_timed_out(item: Any, snapshot: _CaseSnapshot) -> bool:
    return request_is_effectively_timed_out(
        item,
        now=snapshot.current,
        has_required_evidence=snapshot.has_required_evidence.get(item.request_id, False),
    )


def _is_completed(item: Any, snapshot: _CaseSnapshot) -> bool:
    return (
        not is_blocked_request_status(item.status)
        and not is_declined_request_status(item.status)
        and snapshot.has_required_evidence.get(item.request_id, False)
    )


def _missing_evidence_request_ids(snapshot: _CaseSnapshot) -> list[str]:
    return [
        item.request_id
        for item in snapshot.requests
        if item.request_id and not snapshot.has_required_evidence.get(item.request_id, False)
    ]


def _sum_status(statuses: list[dict[str, Any]], key: str) -> int:
    return sum(int(status.get(key) or 0) for status in statuses)


def _case_window_status(status: dict[str, Any]) -> str:
    return case_window_status(case_status_text(status))


def _missing_count(statuses: list[dict[str, Any]]) -> int:
    return sum(
        len(ids)
        for ids in (status.get("missing_evidence_request_ids") for status in statuses)
        if isinstance(ids, list)
    )


def _overview_blocker(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": row["case_id"],
        "title": row["title"],
        "reason": "collaboration_ready_for_main_agent",
        "blocked_request_count": row["blocked_request_count"],
        "timed_out_request_count": row.get("timed_out_request_count", 0),
        "missing_evidence_request_count": row["missing_evidence_request_count"],
    }


def _readiness(blockers: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ready": not blockers,
        "blocker_count": len(blockers),
        "blockers": blockers[-20:],
        "next_action": (
            "run_background_main_agent_or_review_ready_cases"
            if blockers
            else "no_collaboration_blockers"
        ),
    }


class CollaborationStore(CollaborationPendingRequestStore):
    """Public collaboration store — the single entry point."""

    def overview(self) -> dict[str, Any]:
        statuses = [self.case_status(case.case_id) for case in self.list_cases()]
        ready_cases = [
            case_overview_row(status)
            for status in statuses
            if bool(status.get("ready_for_main_agent"))
        ]
        blockers = [_overview_blocker(row) for row in ready_cases]
        return {
            "schema_version": "collaboration_overview.v1",
            "case_count": len(statuses),
            "open_case_count": sum(
                1 for status in statuses if _case_window_status(status) == "open"
            ),
            "closed_case_count": sum(
                1 for status in statuses if _case_window_status(status) == "closed"
            ),
            "ready_case_count": len(ready_cases),
            "request_count": _sum_status(statuses, "request_count"),
            "pending_request_count": _sum_status(statuses, "pending_request_count"),
            "blocked_request_count": _sum_status(statuses, "blocked_request_count"),
            "timed_out_request_count": _sum_status(statuses, "timed_out_request_count"),
            "completed_request_count": _sum_status(statuses, "completed_request_count"),
            "declined_request_count": _sum_status(statuses, "declined_request_count"),
            "missing_evidence_request_count": _missing_count(statuses),
            "evidence_count": _sum_status(statuses, "evidence_count"),
            "participant_count": _sum_status(statuses, "participant_count"),
            "decision_count": _sum_status(statuses, "decision_count"),
            "ready_cases": ready_cases[-20:],
            "readiness": _readiness(blockers),
        }

    def case_status(self, case_id: str) -> dict[str, Any]:
        snapshot = _CaseSnapshot.from_store(self, case_id)
        groups = _request_groups(snapshot)
        return _status_payload(snapshot, groups)
