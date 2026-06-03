
from __future__ import annotations

from typing import Any

from ..io.jsonl import append_jsonl
from .models import (
    CaseDecision,
    CaseParticipant,
    EvidencePacket,
    new_evidence_id,
)
from .store_base import CollaborationBaseStore
from .store_common import dict_items, read_jsonl_report, strings
from .store_common import now as current_time


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
