# LLM: Status rendering keeps collaboration state readable without gating the work.
# 模块用途: 汇总 case 的请求、证据、参与者、决策和返工建议，供工具和后台主代理读取。

from __future__ import annotations

from typing import Any

from .request_status import (
    case_overview_row,
    case_status_text,
    evidence_sources_by_request,
    is_blocked_request_status,
    is_declined_request_status,
    missing_responder_agent_ids_by_request,
    request_has_required_evidence,
    request_is_effectively_timed_out,
    unavailable_target_agent_ids_by_request,
)
from .rework import ReworkPlanInput, rework_plan
from .store_common import now
from .store_requests import CollaborationRequestStore


class CollaborationStatusStore(CollaborationRequestStore):
    def overview(self) -> dict[str, Any]:
        statuses = [self.case_status(case.case_id) for case in self.list_cases()]
        ready_cases = [case_overview_row(status) for status in statuses if bool(status.get("ready_for_main_agent"))]
        blockers = [_overview_blocker(row) for row in ready_cases]
        return {
            "schema_version": "collaboration_overview.v1",
            "case_count": len(statuses),
            "open_case_count": sum(1 for status in statuses if case_status_text(status) == "open"),
            "close_case_count": sum(1 for status in statuses if case_status_text(status) == "close"),
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
        rework = rework_plan(ReworkPlanInput(
            blocked_requests=groups["blocked"],
            pending_requests=groups["pending"],
            timed_out_requests=groups["timed_out"],
            evidence_sources_by_request=snapshot.evidence_sources_by_request,
            target_aliases=self.agent_identity_aliases,
        ))
        return _status_payload(snapshot, groups, rework)


class _CaseSnapshot:
    def __init__(self, store: CollaborationStatusStore, case_id: str):
        self.case = store.load_case(case_id)
        self.requests = store.case_requests(case_id)
        self.request_history_count = len(store.case_request_history(case_id))
        self.evidence = store.case_evidence(case_id)
        self.participants = store.case_participants(case_id)
        self.decisions = store.case_decisions(case_id)
        self.current = now()
        self.aliases = store.agent_identity_aliases
        self.evidence_sources_by_request = evidence_sources_by_request(self.evidence, aliases=self.aliases)
        self.has_required_evidence = self._required_evidence_index()

    @classmethod
    def from_store(cls, store: CollaborationStatusStore, case_id: str) -> _CaseSnapshot:
        return cls(store, case_id)

    def _required_evidence_index(self) -> dict[str, bool]:
        return {
            item.request_id: request_has_required_evidence(
                item,
                self.evidence_sources_by_request.get(item.request_id, set()),
                target_aliases=self.aliases,
            )
            for item in self.requests
            if item.request_id
        }


def _request_groups(snapshot: _CaseSnapshot) -> dict[str, list[Any]]:
    return {
        "pending": [item for item in snapshot.requests if _is_pending(item, snapshot)],
        "blocked": [item for item in snapshot.requests if is_blocked_request_status(item.status)],
        "timed_out": [item for item in snapshot.requests if _is_timed_out(item, snapshot)],
        "completed": [item for item in snapshot.requests if _is_completed(item, snapshot)],
        "declined": [item for item in snapshot.requests if is_declined_request_status(item.status)],
    }


def _status_payload(snapshot: _CaseSnapshot, groups: dict[str, list[Any]], rework: dict[str, Any]) -> dict[str, Any]:
    ready = bool(groups["blocked"] or groups["timed_out"] or (not groups["pending"] and (groups["completed"] or snapshot.evidence)))
    return {
        "case": snapshot.case.to_dict(),
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
        "missing_responder_agent_ids_by_request": missing_responder_agent_ids_by_request(snapshot.requests, snapshot.evidence_sources_by_request, target_aliases=snapshot.aliases),
        "unavailable_target_agent_ids_by_request": unavailable_target_agent_ids_by_request(snapshot.requests),
        "ready_for_main_agent": ready,
        "requires_main_agent": ready,
        "rework": rework,
        "rework_targets": rework["targets"],
        **_bounded_case_rows(snapshot, groups),
    }


def _bounded_case_rows(snapshot: _CaseSnapshot, groups: dict[str, list[Any]]) -> dict[str, Any]:
    return {
        "requests": [item.to_dict() for item in snapshot.requests[-10:]],
        "pending_requests": [item.to_dict() for item in groups["pending"][-10:]],
        "blocked_requests": [item.to_dict() for item in groups["blocked"][-10:]],
        "timed_out_requests": [item.to_dict() for item in groups["timed_out"][-10:]],
        "completed_requests": [item.to_dict() for item in groups["completed"][-10:]],
        "declined_requests": [item.to_dict() for item in groups["declined"][-10:]],
        "evidence": [item.to_dict() for item in snapshot.evidence[-10:]],
        "participants": [item.to_dict() for item in snapshot.participants[-20:]],
        "decisions": [item.to_dict() for item in snapshot.decisions[-10:]],
    }


def _is_pending(item: Any, snapshot: _CaseSnapshot) -> bool:
    return not (
        is_blocked_request_status(item.status)
        or _is_timed_out(item, snapshot)
        or is_declined_request_status(item.status)
        or snapshot.has_required_evidence.get(item.request_id, False)
    )


def _is_timed_out(item: Any, snapshot: _CaseSnapshot) -> bool:
    return request_is_effectively_timed_out(item, now=snapshot.current, has_required_evidence=snapshot.has_required_evidence.get(item.request_id, False))


def _is_completed(item: Any, snapshot: _CaseSnapshot) -> bool:
    return not is_blocked_request_status(item.status) and not is_declined_request_status(item.status) and snapshot.has_required_evidence.get(item.request_id, False)


def _missing_evidence_request_ids(snapshot: _CaseSnapshot) -> list[str]:
    return [
        item.request_id
        for item in snapshot.requests
        if item.request_id and not snapshot.has_required_evidence.get(item.request_id, False)
    ]


def _sum_status(statuses: list[dict[str, Any]], key: str) -> int:
    return sum(int(status.get(key) or 0) for status in statuses)


def _missing_count(statuses: list[dict[str, Any]]) -> int:
    return sum(len(ids) for ids in (status.get("missing_evidence_request_ids") for status in statuses) if isinstance(ids, list))


def _overview_blocker(row: dict[str, Any]) -> dict[str, Any]:
    return {"case_id": row["case_id"], "title": row["title"], "reason": "collaboration_ready_for_main_agent", "blocked_request_count": row["blocked_request_count"], "timed_out_request_count": row.get("timed_out_request_count", 0), "missing_evidence_request_count": row["missing_evidence_request_count"]}


def _readiness(blockers: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ready": not blockers, "blocker_count": len(blockers), "blockers": blockers[-20:], "next_action": "run_background_main_agent_or_review_ready_cases" if blockers else "no_collaboration_blockers"}
