from __future__ import annotations

"""In-memory investigation queue for local log-analysis dispatch."""

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

PENDING_INVESTIGATION = "PENDING_INVESTIGATION"
QUEUED = "QUEUED"
DISPATCHED = "DISPATCHED"
AWAITING_REVIEW = "AWAITING_REVIEW"
REVIEWED = "REVIEWED"
REJECTED = "REJECTED"
FAILED = "FAILED"

ACTIVE_STATUSES = {QUEUED, DISPATCHED, AWAITING_REVIEW}


def _new_request_id() -> str:
    return f"logdisp-{uuid.uuid4().hex[:12]}"


@dataclass
class DispatchRequest:
    case_id: str
    role: str = "analyst"
    priority: str = ""
    status: str = PENDING_INVESTIGATION
    reason: str = ""
    request_id: str = field(default_factory=_new_request_id)
    evidence_refs: list[str] = field(default_factory=list)
    case_summary: dict[str, Any] = field(default_factory=dict)
    route_summary: dict[str, Any] = field(default_factory=dict)
    assigned_agent_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PendingInvestigationInput:
    case_id: str
    priority: str = ""
    reason: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    case_summary: dict[str, Any] = field(default_factory=dict)
    route_summary: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> PendingInvestigationInput:
        return cls(
            case_id=str(kwargs.get("case_id", "")),
            priority=str(kwargs.get("priority", "")),
            reason=str(kwargs.get("reason", "")),
            evidence_refs=list(kwargs.get("evidence_refs") or []),
            case_summary=dict(kwargs.get("case_summary") or {}),
            route_summary=dict(kwargs.get("route_summary") or {}),
        )


class InvestigationQueue:
    """Small queue owned by the parent/session health layer."""

    def __init__(self, requests: list[DispatchRequest] | None = None):
        self._requests: list[DispatchRequest] = list(requests or [])

    def __len__(self) -> int:
        return len(self._requests)

    @property
    def requests(self) -> list[DispatchRequest]:
        return list(self._requests)

    def add(self, request: DispatchRequest) -> DispatchRequest:
        request.updated_at = time.time()
        self._requests.append(request)
        return request

    def add_pending(
        self,
        *,
        pending: PendingInvestigationInput | None = None,
        **kwargs: Any,
    ) -> DispatchRequest:
        item = pending or PendingInvestigationInput.from_kwargs(**kwargs)
        return self.add(
            DispatchRequest(
                case_id=item.case_id,
                priority=item.priority,
                reason=item.reason,
                evidence_refs=list(item.evidence_refs),
                case_summary=dict(item.case_summary),
                route_summary=dict(item.route_summary),
            )
        )

    def get(self, request_id: str) -> DispatchRequest | None:
        for request in self._requests:
            if request.request_id == request_id:
                return request
        return None

    def for_case(self, case_id: str) -> list[DispatchRequest]:
        return [request for request in self._requests if request.case_id == case_id]

    def pending(self, *, role: str | None = None) -> list[DispatchRequest]:
        return [
            request
            for request in self._requests
            if request.status == PENDING_INVESTIGATION and (role is None or request.role == role)
        ]

    def active(self, *, role: str | None = None) -> list[DispatchRequest]:
        return [
            request
            for request in self._requests
            if request.status in ACTIVE_STATUSES and (role is None or request.role == role)
        ]

    def dispatched_since(self, since_timestamp: float, *, role: str | None = None) -> list[DispatchRequest]:
        counted_statuses = ACTIVE_STATUSES | {REVIEWED, REJECTED, FAILED}
        return [
            request
            for request in self._requests
            if request.status in counted_statuses
            and request.updated_at >= since_timestamp
            and (role is None or request.role == role)
        ]

    def mark_dispatched(self, request_id: str, *, agent_id: str) -> DispatchRequest:
        request = self.get(request_id)
        if request is None:
            raise KeyError(request_id)
        request.status = DISPATCHED
        request.assigned_agent_id = agent_id
        request.reason = "dispatched"
        request.attempts += 1
        request.updated_at = time.time()
        return request

    def mark_rejected(self, request_id: str, *, reason: str) -> DispatchRequest:
        request = self.get(request_id)
        if request is None:
            raise KeyError(request_id)
        request.status = REJECTED
        request.reason = reason
        request.updated_at = time.time()
        return request

    def counts_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for request in self._requests:
            counts[request.status] = counts.get(request.status, 0) + 1
        return counts

    def agent_backlog(self) -> dict[str, Any]:
        by_status = self.counts_by_status()
        return {
            "total": len(self._requests),
            "pending_investigation": by_status.get(PENDING_INVESTIGATION, 0),
            "queued": by_status.get(QUEUED, 0),
            "dispatched": by_status.get(DISPATCHED, 0),
            "awaiting_review": by_status.get(AWAITING_REVIEW, 0),
            "rejected": by_status.get(REJECTED, 0),
            "active_analyst_agents": len(self.active(role="analyst")),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.agent_backlog(),
            "requests": [request.to_dict() for request in self._requests],
        }
