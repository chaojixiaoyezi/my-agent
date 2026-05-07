# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar

from .model_aliases import SECURITY_ALERT_V1_FIELD_ALIASES

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
T = TypeVar("T", bound="JsonRoundTripMixin")


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 utc_now_iso 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 utc now iso 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _json_ready 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 json ready 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
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


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 JsonRoundTripMixin 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 JsonRoundTripMixin 的状态和协作方法，作为当前模块对外复用的领域对象。
class JsonRoundTripMixin:
    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 to_json 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to json 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 from_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 从外部数据还原 from dict 需要的领域对象，统一缺省值和兼容字段。
    @classmethod
    def from_dict(cls: type[T], values: Mapping[str, Any]) -> T:
        allowed = {item.name for item in fields(cls)}
        clean = {key: value for key, value in values.items() if key in allowed}
        return cls(**clean)

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 from_json 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 从外部数据还原 from json 需要的领域对象，统一缺省值和兼容字段。
    @classmethod
    def from_json(cls: type[T], payload: str | bytes) -> T:
        return cls.from_dict(json.loads(payload))


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 SourceSpec 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SourceSpec 的字段集合，在模块边界间传递结构化状态和结果。
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


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 Checkpoint 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 Checkpoint 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class Checkpoint(JsonRoundTripMixin):
    source_id: str
    cursor_kind: str
    cursor: dict[str, Any] = field(default_factory=dict)
    last_committed_batch_id: str = ""
    last_event_time: str = ""
    updated_at: str = ""


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 RawBatch 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 RawBatch 的字段集合，在模块边界间传递结构化状态和结果。
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


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 NormalizedEvent 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 NormalizedEvent 的字段集合，在模块边界间传递结构化状态和结果。
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


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 SecurityAlertV1 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 SecurityAlertV1 的字段集合，在模块边界间传递结构化状态和结果。
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


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 EvidenceRef 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 EvidenceRef 的字段集合，在模块边界间传递结构化状态和结果。
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

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 __post_init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 post init 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
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


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _coerce_evidence_refs 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce evidence refs 涉及的字段，让后续匹配和存储使用同一形态。
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


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _coerce_evidence_ref 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce evidence ref 涉及的字段，让后续匹配和存储使用同一形态。
def _coerce_evidence_ref(value: Any) -> EvidenceRef | None:
    if isinstance(value, EvidenceRef):
        return value
    if isinstance(value, str):
        return EvidenceRef(evidence_id=value)
    if isinstance(value, Mapping):
        return EvidenceRef.from_dict(value)
    return None


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 QueryPlan 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 QueryPlan 的字段集合，在模块边界间传递结构化状态和结果。
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

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 __post_init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 post init 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
    def __post_init__(self) -> None:
        if not self.display:
            self.display = _query_plan_display(self)


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _query_plan_display 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 query plan display 的候选结果，并按参数完成筛选、排序或数量限制。
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


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 _query_plan_filter_display 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 query plan filter display 的候选结果，并按参数完成筛选、排序或数量限制。
def _query_plan_filter_display(filters: Mapping[str, Any]) -> str:
    if not filters:
        return ""
    return " ".join(f"{key}={value}" for key, value in sorted(filters.items()) if value not in (None, "", [], {}))


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 Finding 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 Finding 的字段集合，在模块边界间传递结构化状态和结果。
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

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 from_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 从外部数据还原 from dict 需要的领域对象，统一缺省值和兼容字段。
    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> Finding:
        allowed = {item.name for item in fields(cls)}
        clean = {key: value for key, value in values.items() if key in allowed}
        clean["evidence_refs"] = _coerce_evidence_refs(clean.get("evidence_refs", []))
        return cls(**clean)


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 CaseRecord 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 CaseRecord 的字段集合，在模块边界间传递结构化状态和结果。
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

    # LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 from_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 从外部数据还原 from dict 需要的领域对象，统一缺省值和兼容字段。
    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> CaseRecord:
        allowed = {item.name for item in fields(cls)}
        clean = {key: value for key, value in values.items() if key in allowed}
        clean["evidence_refs"] = _coerce_evidence_refs(clean.get("evidence_refs", []))
        return cls(**clean)


Case = CaseRecord


from .models_work_orders import LogWorkOrder, QueryResult, SecurityCase

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
