
from __future__ import annotations

"""Contracts for log-analysis analyst and reviewer subagents.

The parent session should exchange only small, auditable objects with these
roles. Raw events, long query output, and transcripts stay behind tools and
evidence references.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any

ANALYST_ROLE = "analyst"
REVIEWER_ROLE = "reviewer"

DEFAULT_ANALYST_TOOLS = [
    "security_query",
    "security_hunt_ip",
    "security_trace_case",
    "evidence_read",
]

DEFAULT_ACCEPTANCE_CHECKS = [
    "Report must cite at least one evidence_ref.",
    "Facts, inferences, and gaps must be separated.",
    "Next actions must be concrete and evidence-driven.",
    "Archive or summary text must not be treated as the final source of truth.",
]


class ContractValidationError(ValueError):
    """Raised when a subagent payload violates the local contract."""


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _to_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        payload = value.to_dict()
        return payload if isinstance(payload, Mapping) else {}
    if is_dataclass(value):
        return asdict(value)
    return {}


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _compact_string(value: Any, *, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _compact_string_list(value: Any, *, limit: int = 50) -> list[str]:
    if value is None:
        return []
    output: list[str] = []
    for item in _list_items(value)[:limit]:
        _append_compact_string(output, item)
    return output


def _list_items(value: Any) -> list[Any]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [str(value)]
    try:
        return list(value)
    except TypeError:
        return [value]


def _append_compact_string(output: list[str], item: Any) -> None:
    text = _compact_string(item)
    if text:
        output.append(text)


def _evidence_ref_from_mapping(value: Mapping[str, Any]) -> str:
    metadata = value.get("metadata")
    ref = _first_evidence_ref(
        value,
        "evidence_ref",
        "evidence_id",
        "ref",
        "id",
        "query_id",
        "path",
        "uri",
    )
    if ref:
        return ref
    if isinstance(metadata, Mapping):
        return _first_evidence_ref(metadata, "evidence_id", "evidence_ref", "ref", "id", "query_id", "path", "evidence_path", "uri")
    return ""


def _first_evidence_ref(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        text = _compact_string(value.get(key), limit=300)
        if text:
            return text
    return ""


def normalize_evidence_refs(value: Any, *, limit: int = 50) -> list[str]:
    """Return compact evidence ref strings from strings or evidence dicts."""

    if value is None:
        return []
    if isinstance(value, str) or _to_mapping(value):
        items = [value]
    else:
        try:
            items = list(value)
        except TypeError:
            items = [value]

    refs: list[str] = []
    seen: set[str] = set()
    for item in items[:limit]:
        mapping = _to_mapping(item)
        if mapping:
            ref = _evidence_ref_from_mapping(mapping)
        else:
            ref = _compact_string(item, limit=300)
        if ref and ref not in seen:
            refs.append(ref)
            seen.add(ref)
    return refs


def require_evidence_refs(evidence_refs: Any, *, field_name: str = "evidence_refs") -> list[str]:
    refs = normalize_evidence_refs(evidence_refs)
    if not refs:
        raise ContractValidationError(f"{field_name} must contain at least one evidence reference")
    return refs


@dataclass
class AnalystInput:
    """Small input object for an analyst subagent."""

    case_id: str
    case_summary: str
    evidence_refs: list[str]
    route_summary: dict[str, Any] = field(default_factory=dict)
    entity_refs: list[str] = field(default_factory=list)
    finding_refs: list[str] = field(default_factory=list)
    available_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ANALYST_TOOLS))
    budget: dict[str, int] = field(default_factory=dict)
    acceptance_checks: list[str] = field(default_factory=lambda: list(DEFAULT_ACCEPTANCE_CHECKS))

    def validate(self) -> None:
        if not self.case_id:
            raise ContractValidationError("case_id is required")
        if not self.case_summary:
            raise ContractValidationError("case_summary is required")
        self.evidence_refs = require_evidence_refs(self.evidence_refs)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> AnalystInput:
        route_summary = _as_mapping(payload.get("route_summary") or payload.get("route"))
        item = cls(
            case_id=_compact_string(payload.get("case_id") or payload.get("id"), limit=120),
            case_summary=_compact_string(payload.get("case_summary") or payload.get("summary")),
            evidence_refs=normalize_evidence_refs(payload.get("evidence_refs") or payload.get("evidence")),
            route_summary=dict(route_summary),
            entity_refs=_compact_string_list(payload.get("entity_refs") or payload.get("entities")),
            finding_refs=_compact_string_list(payload.get("finding_refs") or payload.get("findings")),
            available_tools=_compact_string_list(payload.get("available_tools")) or list(DEFAULT_ANALYST_TOOLS),
            budget=dict(_as_mapping(payload.get("budget"))),
            acceptance_checks=_compact_string_list(payload.get("acceptance_checks")) or list(DEFAULT_ACCEPTANCE_CHECKS),
        )
        item.validate()
        return item


@dataclass
class AnalystReport:
    """Analyst output contract consumed by the reviewer."""

    case_id: str
    summary: str
    evidence_refs: list[str]
    facts: list[str] = field(default_factory=list)
    inferences: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    confidence: str = "medium"
    status: str = "AWAITING_REVIEW"

    def validate(self) -> None:
        if not self.case_id:
            raise ContractValidationError("case_id is required")
        if not self.summary:
            raise ContractValidationError("summary is required")
        self.evidence_refs = require_evidence_refs(self.evidence_refs)
        if not (self.facts or self.inferences or self.gaps):
            raise ContractValidationError("report must separate facts, inferences, or gaps")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> AnalystReport:
        item = cls(
            case_id=_compact_string(payload.get("case_id") or payload.get("id"), limit=120),
            summary=_compact_string(payload.get("summary")),
            evidence_refs=normalize_evidence_refs(payload.get("evidence_refs") or payload.get("evidence")),
            facts=_compact_string_list(payload.get("facts")),
            inferences=_compact_string_list(payload.get("inferences") or payload.get("hypotheses")),
            gaps=_compact_string_list(payload.get("gaps")),
            next_actions=_compact_string_list(payload.get("next_actions") or payload.get("next_queries")),
            confidence=_compact_string(payload.get("confidence") or "medium", limit=40),
            status=_compact_string(payload.get("status") or "AWAITING_REVIEW", limit=40),
        )
        item.validate()
        return item


@dataclass
class ReviewerInput:
    """Reviewer input: a report plus the known evidence boundary."""

    case_id: str
    analyst_report: AnalystReport
    evidence_refs: list[str] = field(default_factory=list)
    acceptance_checks: list[str] = field(default_factory=lambda: list(DEFAULT_ACCEPTANCE_CHECKS))

    def validate(self) -> None:
        if not self.case_id:
            raise ContractValidationError("case_id is required")
        self.analyst_report.validate()
        self.evidence_refs = normalize_evidence_refs(self.evidence_refs) or list(self.analyst_report.evidence_refs)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "case_id": self.case_id,
            "analyst_report": self.analyst_report.to_dict(),
            "evidence_refs": list(self.evidence_refs),
            "acceptance_checks": list(self.acceptance_checks),
        }


@dataclass
class ReviewerDecision:
    """Reviewer output contract."""

    case_id: str
    approved: bool
    decision: str
    reasons: list[str]
    evidence_refs: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_analyst_input(payload: AnalystInput | Mapping[str, Any]) -> AnalystInput:
    if isinstance(payload, AnalystInput):
        payload.validate()
        return payload
    return AnalystInput.from_mapping(payload)


def validate_analyst_report(payload: AnalystReport | Mapping[str, Any]) -> AnalystReport:
    if isinstance(payload, AnalystReport):
        payload.validate()
        return payload
    return AnalystReport.from_mapping(payload)
