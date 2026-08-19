
from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Any

from .models import CollaborationCase, CollaborationRequest, EvidencePacket
from .store_common import float_value

TargetIdentityKeys = Callable[[object], set[str]]


class CollaborationCaseStatus(str, Enum):
    """Current collaboration case lifecycle values understood by machines."""

    OPEN = "open"
    CLOSED = "closed"


class CollaborationRequestStatus(str, Enum):
    """Current collaboration request lifecycle values understood by machines."""

    OPEN = "open"
    PENDING = "pending"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    DECLINED = "declined"


class CollaborationResponseStatus(str, Enum):
    """Derived response buckets; not written back as request status."""

    RESPONDED = "responded"
    UNAVAILABLE = "unavailable"
    UNANSWERED = "unanswered"
    WAITING = "waiting"


class CollaborationPriority(str, Enum):
    """Current collaboration priority values that carry runtime semantics."""

    NORMAL = "normal"
    URGENT = "urgent"


COLLABORATION_CASE_STATUS_INVALID = "COLLABORATION_CASE_STATUS_INVALID"
COLLABORATION_REQUEST_STATUS_INVALID = "COLLABORATION_REQUEST_STATUS_INVALID"


def normalize_case_status(status: str) -> str:
    text = _status_text(status)
    return text if text in _CASE_STATUS_VALUES else ""


def normalize_request_status(status: str) -> str:
    text = _status_text(status)
    return text if text in _REQUEST_STATUS_VALUES else ""


def case_status_for_update(status: str, current_status: str) -> str:
    """Return an exact protocol status, preserving current state for invalid text."""
    return (
        normalize_case_status(status)
        or normalize_case_status(current_status)
        or CollaborationCaseStatus.OPEN.value
    )


def request_status_for_update(status: str, current_status: str) -> str:
    """Return an exact protocol status, preserving current state for invalid text."""
    return (
        normalize_request_status(status)
        or normalize_request_status(current_status)
        or CollaborationRequestStatus.PENDING.value
    )


def case_status_protocol_metadata(status: str) -> dict[str, str]:
    text = _status_text(status)
    if not text or normalize_case_status(text):
        return {}
    return {
        "raw_case_status": text,
        "case_status_protocol_error": COLLABORATION_CASE_STATUS_INVALID,
    }


def request_status_protocol_metadata(status: str) -> dict[str, str]:
    text = _status_text(status)
    if not text or normalize_request_status(text):
        return {}
    return {
        "raw_request_status": text,
        "request_status_protocol_error": COLLABORATION_REQUEST_STATUS_INVALID,
    }


def normalize_runtime_priority(priority: object) -> str:
    text = _status_text(priority)
    return text if text in _RUNTIME_PRIORITY_VALUES else CollaborationPriority.NORMAL.value


def is_urgent_priority(priority: object) -> bool:
    return normalize_runtime_priority(priority) == CollaborationPriority.URGENT.value


def is_terminal_status(status: str) -> bool:
    return normalize_case_status(status) == CollaborationCaseStatus.CLOSED.value


def case_window_status(status: str) -> str:
    return CollaborationCaseStatus.CLOSED.value if is_terminal_status(status) else CollaborationCaseStatus.OPEN.value


def is_completed_request_status(status: str) -> bool:
    return normalize_request_status(status) == CollaborationRequestStatus.COMPLETED.value


def is_blocked_request_status(status: str) -> bool:
    text = normalize_request_status(status)
    if is_timed_out_request_status(text):
        return False
    return text == CollaborationRequestStatus.BLOCKED.value


def is_timed_out_request_status(status: str) -> bool:
    return normalize_request_status(status) == CollaborationRequestStatus.TIMEOUT.value


def is_declined_request_status(status: str) -> bool:
    return normalize_request_status(status) == CollaborationRequestStatus.DECLINED.value


def _status_text(status: str) -> str:
    return str(status or "").strip()


_CASE_STATUS_VALUES = frozenset(status.value for status in CollaborationCaseStatus)
_REQUEST_STATUS_VALUES = frozenset(status.value for status in CollaborationRequestStatus)
_RUNTIME_PRIORITY_VALUES = frozenset(status.value for status in CollaborationPriority)


def request_is_effectively_timed_out(
    request: CollaborationRequest,
    *,
    now: float,
    has_required_evidence: bool,
) -> bool:
    if has_required_evidence:
        return False
    return is_timed_out_request_status(request.status) or request_deadline_expired(request, now)


def request_deadline_expired(request: CollaborationRequest, now: float) -> bool:
    deadline = float_value(getattr(request, "deadline_at", 0.0))
    return bool(deadline > 0 and deadline <= now)


def request_response_status(
    request: CollaborationRequest,
    *,
    now: float,
    has_required_evidence: bool,
) -> str:
    if has_required_evidence:
        return CollaborationResponseStatus.RESPONDED.value
    if is_blocked_request_status(request.status) or is_declined_request_status(request.status):
        return CollaborationResponseStatus.UNAVAILABLE.value
    if request_is_effectively_timed_out(request, now=now, has_required_evidence=False):
        return CollaborationResponseStatus.UNANSWERED.value
    return CollaborationResponseStatus.WAITING.value


def evidence_sources_by_request(evidence: list[EvidencePacket], *, identity_keys: TargetIdentityKeys) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for item in evidence:
        request_id = str(getattr(item, "request_id", "") or "")
        source = str(getattr(item, "source_agent_id", "") or "")
        if request_id and source:
            result.setdefault(request_id, set()).update(identity_keys(source))
    return result


def request_has_required_evidence(
    request: CollaborationRequest,
    evidence_sources: set[str],
    *,
    target_identity_keys: TargetIdentityKeys,
) -> bool:
    targets = [str(item) for item in request.target_agent_ids if str(item or "").strip()]
    if len(targets) <= 1:
        return bool(evidence_sources)
    return bool(evidence_sources) and all(
        bool(evidence_sources.intersection(target_identity_keys(target))) for target in targets
    )


def missing_responder_agent_ids(
    request: CollaborationRequest,
    evidence_sources: set[str],
    *,
    target_identity_keys: TargetIdentityKeys,
) -> list[str]:
    missing: list[str] = []
    for target in [str(item) for item in request.target_agent_ids if str(item or "").strip()]:
        if not evidence_sources.intersection(target_identity_keys(target)):
            missing.append(target)
    return list(dict.fromkeys(missing))


def missing_responder_agent_ids_by_request(
    requests: list[CollaborationRequest],
    evidence_sources: dict[str, set[str]],
    *,
    target_identity_keys: TargetIdentityKeys,
) -> dict[str, list[str]]:
    rows = (
        (request.request_id, missing_responder_agent_ids(request, evidence_sources.get(request.request_id, set()), target_identity_keys=target_identity_keys))
        for request in requests
    )
    return {request_id: missing for request_id, missing in rows if missing}


def unavailable_target_agent_ids_by_request(
    requests: list[CollaborationRequest],
) -> dict[str, list[str]]:
    rows = ((request.request_id, unavailable_target_agent_ids(request)) for request in requests)
    return {request_id: unavailable for request_id, unavailable in rows if unavailable}


def unavailable_target_agent_ids(request: CollaborationRequest) -> list[str]:
    rows = request.metadata.get("unavailable_targets") if isinstance(request.metadata, dict) else None
    if not isinstance(rows, list):
        return []
    return list(dict.fromkeys(_unavailable_agent_id(row) for row in rows if _unavailable_agent_id(row)))


def pending_request_row(case: CollaborationCase, request: CollaborationRequest) -> dict[str, Any]:
    """Small responder-facing handoff shape; heavy context remains referenced."""
    return {
        "case_id": case.case_id,
        "request_id": request.request_id,
        "case_ref": f"collaboration://case/{case.case_id}",
        "request_ref": f"collaboration://request/{request.request_id}",
        "case_title": case.title,
        "case_status": case.status,
        "question": request.question,
        "status": request.status,
        "priority": request.priority or case.priority,
        "target_agent_ids": list(request.target_agent_ids),
        "required_capabilities": list(request.required_capabilities),
        "entities": dict(request.entities or {}),
        "problem_statement": request.problem_statement,
        "observed_facts": list(request.observed_facts),
        "query_intent": dict(request.query_intent or {}),
        "query_hints": list(request.query_hints),
        "routing_requirements": dict(request.routing_requirements or {}),
        "response_contract": dict(request.response_contract or {}),
        "context_refs": list(request.context_refs),
        "created_at": request.created_at,
        "updated_at": request.updated_at,
        "recommended_tools": [],
        "next_action_zh": "在 SUBAGENT_RESULT 的证据/结论里原样引用 case_ref/request_ref 回应；有命中就提交证据，没有命中也提交未命中说明。",
    }


def case_overview_row(status: dict[str, Any]) -> dict[str, Any]:
    case = status.get("case") if isinstance(status.get("case"), dict) else {}
    missing_ids = status.get("missing_evidence_request_ids")
    return {
        "case_id": str(case.get("case_id") or ""),
        "title": str(case.get("title") or ""),
        "status": str(case.get("status") or ""),
        "priority": str(case.get("priority") or ""),
        "thread_id": str(case.get("thread_id") or ""),
        "task_id": str(case.get("task_id") or ""),
        "request_count": int(status.get("request_count") or 0),
        "pending_request_count": int(status.get("pending_request_count") or 0),
        "blocked_request_count": int(status.get("blocked_request_count") or 0),
        "timed_out_request_count": int(status.get("timed_out_request_count") or 0),
        "completed_request_count": int(status.get("completed_request_count") or 0),
        "missing_evidence_request_count": len(missing_ids) if isinstance(missing_ids, list) else 0,
        "evidence_count": int(status.get("evidence_count") or 0),
    }


def case_status_text(status: dict[str, Any]) -> str:
    case = status.get("case") if isinstance(status.get("case"), dict) else {}
    return str(case.get("status") or "")


def _unavailable_agent_id(row: object) -> str:
    if isinstance(row, dict):
        return str(row.get("agent_id") or row.get("run_id") or "").strip()
    return str(row or "").strip()
