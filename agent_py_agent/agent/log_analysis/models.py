
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
T = TypeVar("T", bound="JsonRoundTripMixin")

SECURITY_ALERT_V1_FIELD_NAME_MAP: dict[str, str] = {
    "告警类型": "alert_type",
    "威胁名称": "threat_name",
    "IOC/规则ID": "ioc_or_rule_id",
    "URI": "uri",
    "XFF代理": "xff_proxy",
    "Payload": "payload",
    "域名": "domain",
    "referer": "referer",
    "目的端口": "dst_port",
    "协议": "protocol",
    "受害资产组": "victim_asset_group",
    "攻击资产组": "attacker_asset_group",
    "受害IP": "victim_ip",
    "攻击IP": "attacker_ip",
    "源IP": "src_ip",
    "目的IP": "dst_ip",
    "检测位置": "detection_location",
    "检测字段": "detection_field",
    "匹配": "match_operator",
    "值": "matched_value",
    "设备序列号": "device_serial_number",
    "告警规则": "alert_rule",
    "API": "api",
    "API威胁类型": "api_threat_type",
    "OWASP类型": "owasp_type",
}


def utc_now_iso() -> str:
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
            self.display = _query_plan_display(self)


def _query_plan_display(plan: QueryPlan) -> str:
    parts = [plan.purpose]
    if plan.source_products:
        parts.append(f"sources={','.join(plan.source_products)}")
    if plan.start_time or plan.end_time:
        parts.append(f"time={plan.start_time or '*'}..{plan.end_time or '*'}")
    filters = _query_plan_filter_display(plan.filters)
    if filters:
        parts.append(filters)
    return " | ".join(part for part in parts if part)


def _query_plan_filter_display(filters: Mapping[str, Any]) -> str:
    if not filters:
        return ""
    return " ".join(f"{key}={value}" for key, value in sorted(filters.items()) if value not in (None, "", [], {}))


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
