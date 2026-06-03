
from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..gateway_parts.io import read_json_file
from .models import (
    CaseDecision,
    CollaborationCase,
    new_case_id,
    new_decision_id,
)
from .request_status import is_terminal_status
from .store_capabilities import CollaborationCapabilityStore
from .store_common import now as current_time
from .store_common import strings


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
            self.add_participant({"case_id": case.case_id, "agent_id": case.created_by, "role": "creator", "status": "joined", "now": current})
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

    def update_case_status(self, case_id: str, *, status: str, now: float | None = None) -> CollaborationCase:
        case = self.load_case(case_id)
        updated = replace(case, status=str(status or case.status), updated_at=now if now is not None else __import__("time").time())
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
        self._validate_terminal_case_status(case_id, status_text, kwargs)
        updated = self.update_case_status(case_id, status=status_text, now=kwargs.get("now"))
        self._append_status_decision(case_id, status_text, kwargs)
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
