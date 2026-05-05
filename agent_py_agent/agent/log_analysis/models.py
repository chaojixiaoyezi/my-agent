from __future__ import annotations

"""Core DTOs for the optional log analysis module.

These dataclasses are intentionally storage-agnostic and dependency-free.  They
describe what flows between sources, parsers, stores, detectors, cases, and
agents; later workers can plug in concrete ingestion and query backends without
changing the contract.
"""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar

from .model_aliases import SECURITY_ALERT_V1_FIELD_ALIASES

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
T = TypeVar("T", bound="JsonRoundTripMixin")


def utc_now_iso() -> str:
    """Return a compact UTC timestamp suitable for case and evidence records."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return _json_ready(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


class JsonRoundTripMixin:
    """Small helper for JSON-ready dataclass contracts."""

    def to_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls: type[T], values: Mapping[str, Any]) -> T:
        allowed = {item.name for item in fields(cls)}
        clean = {key: value for key, value in values.items() if key in allowed}
        return cls(**clean)

    @classmethod
    def from_json(cls: type[T], payload: str | bytes) -> T:
        return cls.from_dict(json.loads(payload))


@dataclass
class SourceSpec(JsonRoundTripMixin):
    source_id: str
    kind: str = "file"
    format: str = "jsonl"
    enabled: bool = True
    priority: str = "normal"
    parser_id: str = "auto"
    dedup_policy: str = "source_event_fingerprint"
    checkpoint_policy: str = "after_durable_write"
    retention_days: int = 30
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Checkpoint(JsonRoundTripMixin):
    source_id: str
    cursor_kind: str
    cursor: dict[str, Any] = field(default_factory=dict)
    last_committed_batch_id: str = ""
    last_event_time: str = ""
    updated_at: str = ""


@dataclass
class RawBatch(JsonRoundTripMixin):
    batch_id: str
    source_id: str
    source_kind: str
    received_at: str
    time_range: list[str] = field(default_factory=list)
    raw_refs: list[str] = field(default_factory=list)
    size_bytes: int = 0
    content_hash: str = ""
    cursor_before: dict[str, Any] = field(default_factory=dict)
    cursor_after: dict[str, Any] = field(default_factory=dict)
    status: str = "received"
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedEvent(JsonRoundTripMixin):
    event_id: str
    source_id: str
    event_time: str
    ingest_time: str = ""
    event_type: str = "custom"
    src_ip: str = ""
    dst_ip: str = ""
    src_port: int | None = None
    dst_port: int | None = None
    protocol: str = ""
    bytes_out: int | None = None
    bytes_in: int | None = None
    raw_ref: str = ""
    raw_line_no: int | None = None
    parser_id: str = ""
    parser_confidence: float | None = None
    dedup_key: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class SecurityAlertV1(JsonRoundTripMixin):
    alert_id: str
    event_time: str
    source_id: str
    source_product: str = ""
    alert_type: str = ""
    threat_name: str = ""
    ioc_or_rule_id: str = ""
    uri: str = ""
    xff_proxy: str = ""
    payload: str = ""
    domain: str = ""
    referer: str = ""
    dst_port: int | None = None
    protocol: str = ""
    victim_asset_group: str = ""
    attacker_asset_group: str = ""
    victim_ip: str = ""
    attacker_ip: str = ""
    src_ip: str = ""
    dst_ip: str = ""
    detection_location: str = ""
    detection_field: str = ""
    match_operator: str = ""
    matched_value: str = ""
    device_serial_number: str = ""
    alert_rule: str = ""
    api: str = ""
    api_threat_type: str = ""
    owasp_type: str = ""
    raw_ref: str = ""
    raw_fields: dict[str, Any] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvidenceRef(JsonRoundTripMixin):
    evidence_id: str
    kind: str = "query"
    uri: str = ""
    path: str = ""
    source_id: str = ""
    query_id: str = ""
    raw_ref: str = ""
    sample_ref: str = ""
    time_range: list[str] = field(default_factory=list)
    content_hash: str = ""
    sha256: str = ""
    row_count: int = 0
    truncated: bool = False
    created_at: str = field(default_factory=utc_now_iso)
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.path:
            self.path = str(self.metadata.get("evidence_path") or self.metadata.get("path") or "")
        if not self.sha256:
            self.sha256 = str(self.metadata.get("sha256") or self.content_hash or "")
        if not self.content_hash:
            self.content_hash = self.sha256
        if self.path and "evidence_path" not in self.metadata:
            self.metadata["evidence_path"] = self.path
        if self.sha256 and "sha256" not in self.metadata:
            self.metadata["sha256"] = self.sha256


def _coerce_evidence_refs(value: Any) -> list[EvidenceRef]:
    if value is None:
        return []
    if isinstance(value, EvidenceRef):
        return [value]
    if isinstance(value, str):
        return [EvidenceRef(evidence_id=value)]
    if isinstance(value, Mapping):
        return [EvidenceRef.from_dict(value)]

    return [_coerce_evidence_ref(item) for item in value if _coerce_evidence_ref(item) is not None]


def _coerce_evidence_ref(value: Any) -> EvidenceRef | None:
    if isinstance(value, EvidenceRef):
        return value
    if isinstance(value, str):
        return EvidenceRef(evidence_id=value)
    if isinstance(value, Mapping):
        return EvidenceRef.from_dict(value)
    return None


@dataclass
class QueryPlan(JsonRoundTripMixin):
    purpose: str
    source_products: list[str] = field(default_factory=list)
    start_time: str = ""
    end_time: str = ""
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 100
    evidence_needed: list[str] = field(default_factory=list)
    display: str = ""

    def __post_init__(self) -> None:
        if not self.display:
            parts = [self.purpose]
            if self.source_products:
                parts.append(f"sources={','.join(self.source_products)}")
            if self.start_time or self.end_time:
                parts.append(f"time={self.start_time or '*'}..{self.end_time or '*'}")
            if self.filters:
                filters = " ".join(f"{key}={value}" for key, value in sorted(self.filters.items()) if value not in (None, "", [], {}))
                if filters:
                    parts.append(filters)
            self.display = " | ".join(part for part in parts if part)


@dataclass
class Finding(JsonRoundTripMixin):
    finding_id: str
    detector_id: str
    detector_kind: str = "rule"
    window: list[str] = field(default_factory=list)
    severity_hint: str = "low"
    risk_score: float = 0.0
    entities: dict[str, list[str]] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    status: str = "OPEN"
    hypothesis: str = ""
    confidence: float | None = None
    gaps: list[str] = field(default_factory=list)
    next_queries: list[Any] = field(default_factory=list)
    rule_version: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    attributes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> Finding:
        allowed = {item.name for item in fields(cls)}
        clean = {key: value for key, value in values.items() if key in allowed}
        clean["evidence_refs"] = _coerce_evidence_refs(clean.get("evidence_refs", []))
        return cls(**clean)


@dataclass
class CaseRecord(JsonRoundTripMixin):
    case_id: str
    title: str
    status: str = "OPEN"
    priority: str = "P3"
    risk_score: float = 0.0
    finding_refs: list[str] = field(default_factory=list)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    assigned_run_ids: list[str] = field(default_factory=list)
    dedup_key: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    case_type: str = ""
    entities: dict[str, list[str]] = field(default_factory=dict)
    facts: list[str] = field(default_factory=list)
    inferences: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    next_queries: list[Any] = field(default_factory=list)
    route_refs: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> CaseRecord:
        allowed = {item.name for item in fields(cls)}
        clean = {key: value for key, value in values.items() if key in allowed}
        clean["evidence_refs"] = _coerce_evidence_refs(clean.get("evidence_refs", []))
        return cls(**clean)


Case = CaseRecord


@dataclass
class SecurityCase(JsonRoundTripMixin):
    """安全检测器产出的 case 模型。

    由检测器创建，包含基本的案件信息、触发实体、初始证据和来源。
    用于桥接到 LogWorkOrder 并最终转换为 SubAgentTask。
    """
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
    """日志补查工单模型。

    从 SecurityCase 创建，包含补查目标、时间窗口、查询限制和证据预算。
    负责桥接到 SubAgentTask 执行系统。
    """
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
    """受控查询的结果。

    包含查询内容、结果列表、数量统计和截断标志。
    """
    query_template: str
    query_params: dict[str, Any] = field(default_factory=dict)
    time_window: dict[str, str] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    result_count: int = 0
    truncated: bool = False
    max_limit: int = 100
    created_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "Case",
    "CaseRecord",
    "Checkpoint",
    "EvidenceRef",
    "Finding",
    "JsonRoundTripMixin",
    "JsonValue",
    "LogWorkOrder",
    "NormalizedEvent",
    "QueryPlan",
    "QueryResult",
    "RawBatch",
    "SECURITY_ALERT_V1_FIELD_ALIASES",
    "SecurityAlertV1",
    "SecurityCase",
    "SourceSpec",
    "utc_now_iso",
]
