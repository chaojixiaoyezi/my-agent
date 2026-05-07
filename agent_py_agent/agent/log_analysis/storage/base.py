# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Shared contracts for the local log-analysis storage layer."""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypeVar, runtime_checkable

try:  # Worker A owns these models; keep this module compatible while it lands.
    from ..models import Case, EvidenceRef, Finding, NormalizedEvent
except Exception:  # pragma: no cover - exercised only until Worker A's models exist.

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 NormalizedEvent 前先核对字段语义、序列化形态和调用方假设。
    # 类用途: 承载 NormalizedEvent 的字段集合，在模块边界间传递结构化状态和结果。
    @dataclass
    class NormalizedEvent:
        event_id: str
        source_id: str = ""
        event_time: str = ""
        ingest_time: str = ""
        event_type: str = "custom"
        raw_ref: str = ""
        attributes: dict[str, Any] | None = None

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 Finding 前先核对字段语义、序列化形态和调用方假设。
    # 类用途: 承载 Finding 的字段集合，在模块边界间传递结构化状态和结果。
    @dataclass
    class Finding:
        finding_id: str
        detector_id: str = ""
        severity_hint: str = ""
        risk_score: float = 0.0
        evidence_refs: list[str] | None = None
        status: str = "OPEN"
        attributes: dict[str, Any] | None = None

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 Case 前先核对字段语义、序列化形态和调用方假设。
    # 类用途: 承载 Case 的字段集合，在模块边界间传递结构化状态和结果。
    @dataclass
    class Case:
        case_id: str
        title: str = ""
        status: str = "OPEN"
        priority: str = "P3"
        risk_score: float = 0.0
        finding_refs: list[str] | None = None
        evidence_refs: list[str] | None = None
        created_at: str = ""
        updated_at: str = ""
        attributes: dict[str, Any] | None = None

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 EvidenceRef 前先核对字段语义、序列化形态和调用方假设。
    # 类用途: 承载 EvidenceRef 的字段集合，在模块边界间传递结构化状态和结果。
    @dataclass
    class EvidenceRef:
        evidence_id: str
        kind: str = "query_result"
        query_id: str = ""
        uri: str = ""
        path: str = ""
        content_hash: str = ""
        sha256: str = ""
        row_count: int = 0
        truncated: bool = False
        created_at: str = ""
        summary: str = ""
        metadata: dict[str, Any] | None = None


DEFAULT_QUERY_LIMIT = 100
MAX_QUERY_LIMIT = 500
DEFAULT_PREVIEW_LIMIT = 10
SUPPORTED_QUERY_FIELDS = (
    "attacker_ip",
    "victim_ip",
    "domain",
    "uri",
    "alert_type",
    "start_time",
    "end_time",
    "limit",
)

EVENT_ID_FIELDS = ("event_id", "alert_id", "dedup_key")
FINDING_ID_FIELDS = ("finding_id", "dedup_key")
CASE_ID_FIELDS = ("case_id", "dedup_key")
EVIDENCE_ID_FIELDS = ("evidence_id", "ref_id", "query_id")

T = TypeVar("T")


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 QueryCriteria 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 QueryCriteria 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class QueryCriteria:
    attacker_ip: str | None = None
    victim_ip: str | None = None
    domain: str | None = None
    uri: str | None = None
    alert_type: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    limit: int | None = DEFAULT_QUERY_LIMIT


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 QueryRecord 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 QueryRecord 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class QueryRecord:
    query_id: str
    parameters: dict[str, Any]
    row_count: int
    truncated: bool
    evidence_path: str
    duration_ms: int
    created_at: str
    summary: dict[str, Any]


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 QueryResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 QueryResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class QueryResult:
    query_id: str
    parameters: dict[str, Any]
    row_count: int
    truncated: bool
    evidence_path: str
    evidence_ref: EvidenceRef
    duration_ms: int
    summary: dict[str, Any]
    rows: list[dict[str, Any]]
    preview_rows: list[dict[str, Any]]


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 JsonlReadAudit 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 JsonlReadAudit 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class JsonlReadAudit:
    path: str
    read_at: str
    total_lines: int = 0
    blank_lines: int = 0
    valid_records: int = 0
    corrupt_lines: int = 0
    non_object_lines: int = 0
    skipped_lines: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 LogAnalysisStore 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 声明 LogAnalysisStore 的接口契约，让调用方依赖方法签名而非具体实现。
@runtime_checkable
class LogAnalysisStore(Protocol):
    root: Path

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 upsert_events 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 upsert events 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    def upsert_events(self, events: Iterable[NormalizedEvent | dict[str, Any]]) -> int:
        ...

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 list_events 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 list events 的候选结果，并按参数完成筛选、排序或数量限制。
    def list_events(self) -> list[dict[str, Any]]:
        ...

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 upsert_findings 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 upsert findings 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    def upsert_findings(self, findings: Iterable[Finding | dict[str, Any]]) -> int:
        ...

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 list_findings 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 list findings 的候选结果，并按参数完成筛选、排序或数量限制。
    def list_findings(self) -> list[dict[str, Any]]:
        ...

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 upsert_cases 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 upsert cases 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    def upsert_cases(self, cases: Iterable[Case | dict[str, Any]]) -> int:
        ...

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 list_cases 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 list cases 的候选结果，并按参数完成筛选、排序或数量限制。
    def list_cases(self) -> list[dict[str, Any]]:
        ...

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 upsert_evidence_refs 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 计算 upsert evidence refs 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
    def upsert_evidence_refs(self, refs: Iterable[EvidenceRef | dict[str, Any]]) -> int:
        ...

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 list_evidence_refs 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 list evidence refs 的候选结果，并按参数完成筛选、排序或数量限制。
    def list_evidence_refs(self) -> list[dict[str, Any]]:
        ...

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 save_query_record 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 save query record 相关记录，集中处理目标路径、格式化和状态更新。
    def save_query_record(self, record: QueryRecord | dict[str, Any]) -> None:
        ...

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 list_query_records 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 list query records 的候选结果，并按参数完成筛选、排序或数量限制。
    def list_query_records(self) -> list[dict[str, Any]]:
        ...


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 utc_now 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 utc now 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 normalize_limit 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 normalize limit 涉及的字段，让后续匹配和存储使用同一形态。
def normalize_limit(limit: int | None, *, max_limit: int | None = None) -> int:
    effective_max = max_limit if max_limit is not None and max_limit > 0 else MAX_QUERY_LIMIT
    requested = DEFAULT_QUERY_LIMIT if limit is None or limit <= 0 else limit
    return min(requested, effective_max)


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 model_to_dict 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 model to dict 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def model_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return _jsonable(value)
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _jsonable(value.model_dump())
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    raise TypeError(f"Unsupported record type: {type(value)!r}")


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 dict_to_model 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 dict to model 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def dict_to_model(model_type: type[T], payload: dict[str, Any]) -> T:
    data = dict(payload)
    if hasattr(model_type, "from_dict") and callable(model_type.from_dict):
        try:
            return model_type.from_dict(data)
        except TypeError:
            pass
    try:
        return model_type(**data)
    except TypeError:
        if is_dataclass(model_type):
            allowed = {field.name for field in fields(model_type)}
            return model_type(**{key: value for key, value in data.items() if key in allowed})
        raise


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 record_identity 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 record identity 相关记录，集中处理目标路径、格式化和状态更新。
def record_identity(payload: dict[str, Any], id_fields: Iterable[str]) -> str:
    for field in id_fields:
        value = nested_get(payload, field)
        if value not in (None, ""):
            return str(value)
    return "sha256:" + stable_digest(payload)


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 stable_digest 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 stable digest 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def stable_digest(payload: Any) -> str:
    data = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 canonical_json 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 canonical json 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def canonical_json(payload: Any) -> str:
    return json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 nested_get 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 nested get 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def nested_get(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload[key]
    for container in ("attributes", "raw_fields", "metadata"):
        child = payload.get(container)
        if isinstance(child, dict) and key in child:
            return child[key]
    return None


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 parse_event_time 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 从外部数据还原 parse event time 需要的领域对象，统一缺省值和兼容字段。
def parse_event_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return _with_timezone(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _with_timezone(parsed)


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 event_time_value 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 event time value 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def event_time_value(payload: dict[str, Any]) -> Any:
    for field in ("event_time", "timestamp", "time", "ingest_time", "created_at"):
        value = nested_get(payload, field)
        if value not in (None, ""):
            return value
    return None


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 evidence_path_from_ref 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 evidence path from ref 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def evidence_path_from_ref(ref: Any) -> str:
    payload = model_to_dict(ref)
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        evidence_path = metadata.get("evidence_path")
        if evidence_path:
            return str(evidence_path)
    for field in ("path", "uri"):
        value = payload.get(field)
        if value:
            return str(value)
    return ""


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 default_log_analysis_root 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 default log analysis root 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def default_log_analysis_root() -> Path:
    return Path.cwd() / "agent_py_agent" / "data" / "log_analysis"


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _with_timezone 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 with timezone 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _with_timezone(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _jsonable 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 jsonable 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value).replace("\\", "/")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value
