from __future__ import annotations

"""Shared contracts for the local log-analysis storage layer."""

import hashlib
import json
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol, TypeVar, runtime_checkable

try:  # Worker A owns these models; keep this module compatible while it lands.
    from ..models import Case, EvidenceRef, Finding, NormalizedEvent
except Exception:  # pragma: no cover - exercised only until Worker A's models exist.

    @dataclass
    class NormalizedEvent:
        event_id: str
        source_id: str = ""
        event_time: str = ""
        ingest_time: str = ""
        event_type: str = "custom"
        raw_ref: str = ""
        attributes: dict[str, Any] | None = None

    @dataclass
    class Finding:
        finding_id: str
        detector_id: str = ""
        severity_hint: str = ""
        risk_score: float = 0.0
        evidence_refs: list[str] | None = None
        status: str = "OPEN"
        attributes: dict[str, Any] | None = None

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


@dataclass(frozen=True)
class QueryCriteria:
    attacker_ip: str | None = None
    victim_ip: str | None = None
    domain: str | None = None
    uri: str | None = None
    alert_type: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    limit: int = DEFAULT_QUERY_LIMIT


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


@runtime_checkable
class LogAnalysisStore(Protocol):
    root: Path

    def upsert_events(self, events: Iterable[NormalizedEvent | dict[str, Any]]) -> int:
        ...

    def list_events(self) -> list[dict[str, Any]]:
        ...

    def upsert_findings(self, findings: Iterable[Finding | dict[str, Any]]) -> int:
        ...

    def list_findings(self) -> list[dict[str, Any]]:
        ...

    def upsert_cases(self, cases: Iterable[Case | dict[str, Any]]) -> int:
        ...

    def list_cases(self) -> list[dict[str, Any]]:
        ...

    def upsert_evidence_refs(self, refs: Iterable[EvidenceRef | dict[str, Any]]) -> int:
        ...

    def list_evidence_refs(self) -> list[dict[str, Any]]:
        ...

    def save_query_record(self, record: QueryRecord | dict[str, Any]) -> None:
        ...

    def list_query_records(self) -> list[dict[str, Any]]:
        ...


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_limit(limit: int | None) -> int:
    if limit is None or limit <= 0:
        return DEFAULT_QUERY_LIMIT
    return min(limit, MAX_QUERY_LIMIT)


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


def record_identity(payload: dict[str, Any], id_fields: Iterable[str]) -> str:
    for field in id_fields:
        value = nested_get(payload, field)
        if value not in (None, ""):
            return str(value)
    return "sha256:" + stable_digest(payload)


def stable_digest(payload: Any) -> str:
    data = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def canonical_json(payload: Any) -> str:
    return json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def nested_get(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload[key]
    for container in ("attributes", "raw_fields", "metadata"):
        child = payload.get(container)
        if isinstance(child, dict) and key in child:
            return child[key]
    return None


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


def event_time_value(payload: dict[str, Any]) -> Any:
    for field in ("event_time", "timestamp", "time", "ingest_time", "created_at"):
        value = nested_get(payload, field)
        if value not in (None, ""):
            return value
    return None


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


def default_log_analysis_root() -> Path:
    return Path.cwd() / "agent_py_agent" / "data" / "log_analysis"


def _with_timezone(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value
