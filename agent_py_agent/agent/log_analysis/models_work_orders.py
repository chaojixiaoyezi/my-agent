from __future__ import annotations

"""Work-order and query DTOs exported by log_analysis.models."""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from .models import EvidenceRef, JsonRoundTripMixin, _coerce_evidence_refs, utc_now_iso


@dataclass
class SecurityCase(JsonRoundTripMixin):
    """Security detector output used to seed a log-analysis work order."""

    case_id: str
    severity: str = "medium"
    event_class: str = "alert"
    trigger_entities: dict[str, list[str]] = field(default_factory=dict)
    initial_evidence: list[EvidenceRef] = field(default_factory=list)
    detector_id: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    attributes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> SecurityCase:
        allowed = {item.name for item in fields(cls)}
        clean = {key: value for key, value in values.items() if key in allowed}
        clean["initial_evidence"] = _coerce_evidence_refs(clean.get("initial_evidence", []))
        return cls(**clean)


@dataclass
class LogWorkOrder(JsonRoundTripMixin):
    """Bounded investigation request produced from a SecurityCase."""

    work_order_id: str
    case_id: str
    investigation_goal: str
    start_time: str
    end_time: str
    allowed_query_templates: list[str] = field(default_factory=list)
    max_results: int = 100
    evidence_budget: int = 1000
    created_at: str = field(default_factory=utc_now_iso)
    status: str = "OPEN"
    assigned_run_id: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class QueryResult(JsonRoundTripMixin):
    """Controlled-query result plus truncation and metadata."""

    query_template: str
    query_params: dict[str, Any] = field(default_factory=dict)
    time_window: dict[str, str] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    result_count: int = 0
    truncated: bool = False
    max_limit: int = 100
    created_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = ["LogWorkOrder", "QueryResult", "SecurityCase"]
