from __future__ import annotations

"""Controlled local query engine for log-analysis events."""

import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from ..cases.evidence import LocalEvidenceStore, QueryEvidencePayload
from .base import (
    DEFAULT_PREVIEW_LIMIT,
    QueryCriteria,
    QueryRecord,
    QueryResult,
    dict_to_model,
    event_time_value,
    evidence_path_from_ref,
    model_to_dict,
    nested_get,
    normalize_limit,
    parse_event_time,
    stable_digest,
    utc_now,
)
from .local_store import LocalLogStore

FIELD_ALIASES = {
    "attacker_ip": ("attacker_ip", "src_ip"),
    "victim_ip": ("victim_ip", "dst_ip"),
    "domain": ("domain", "host", "sni", "dns_query"),
    "uri": ("uri", "url", "path", "api"),
    "alert_type": ("alert_type", "event_type"),
}

PREVIEW_FIELDS = (
    "event_id",
    "alert_id",
    "event_time",
    "source_id",
    "source_product",
    "event_type",
    "alert_type",
    "threat_name",
    "attacker_ip",
    "victim_ip",
    "src_ip",
    "dst_ip",
    "domain",
    "uri",
    "api",
    "raw_ref",
)


@dataclass(frozen=True)
class _QuerySummaryInput:
    rows: list[dict[str, Any]]
    parameters: dict[str, Any]
    returned_row_count: int
    truncated: bool
    event_read_audit: dict[str, Any] | None


@dataclass(frozen=True)
class _QueryResultInput:
    query_id: str
    parameters: dict[str, Any]
    row_count: int
    truncated: bool
    evidence_path: str
    evidence: Any
    duration_ms: int
    summary: dict[str, Any]
    limited_rows: list[dict[str, Any]]
    preview_limit: int


@dataclass(frozen=True)
class _WriteQueryEvidenceInput:
    # LLM: Query evidence writes share this small bundle with LocalEvidenceStore.
    query_id: str
    parameters: dict[str, Any]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    summary: dict[str, Any]


@dataclass(frozen=True)
class SecurityQueryOptions:
    require_time_range: bool = True
    preview_limit: int = DEFAULT_PREVIEW_LIMIT
    max_limit: int | None = None


def execute_security_query(
    store: LocalLogStore,
    criteria: QueryCriteria | dict[str, Any],
    *,
    options: SecurityQueryOptions | None = None,
    require_time_range: bool = True,
    preview_limit: int = DEFAULT_PREVIEW_LIMIT,
    max_limit: int | None = None,
) -> QueryResult:
    query_options = options or SecurityQueryOptions(
        require_time_range=bool(require_time_range),
        preview_limit=int(preview_limit),
        max_limit=max_limit,
    )
    query = _criteria(criteria)
    if query_options.require_time_range and (not query.start_time or not query.end_time):
        raise ValueError("start_time and end_time are required for controlled security queries")

    result_payload = _execute_query_payload(store, query, query_options)
    _save_query_record(store, result_payload)
    return _query_result(_QueryResultInput(preview_limit=query_options.preview_limit, **result_payload))


def _execute_query_payload(
    store: LocalLogStore,
    query: QueryCriteria,
    query_options: SecurityQueryOptions,
) -> dict[str, Any]:
    start = time.perf_counter()
    limit = normalize_limit(query.limit, max_limit=query_options.max_limit)
    parameters = _criteria_to_parameters(query, limit)
    rows = _matching_rows(store, query)
    event_read_audit = store.last_read_audit(store.events_path)
    row_count = len(rows)
    truncated = row_count > limit
    limited_rows = rows[:limit]
    summary = _query_summary(_QuerySummaryInput(rows, parameters, len(limited_rows), truncated, event_read_audit))
    query_id = _query_id(parameters)
    evidence = _write_query_evidence(
        store,
        _WriteQueryEvidenceInput(
            query_id=query_id,
            parameters=parameters,
            rows=limited_rows,
            row_count=row_count,
            truncated=truncated,
            summary=summary,
        ),
    )
    store.upsert_evidence_ref(evidence)
    duration_ms = int((time.perf_counter() - start) * 1000)
    evidence_path = evidence_path_from_ref(evidence)
    result_payload = dict(
        query_id=query_id,
        parameters=parameters,
        row_count=row_count,
        truncated=truncated,
        evidence_path=evidence_path,
        evidence=evidence,
        duration_ms=duration_ms,
        summary=summary,
        limited_rows=limited_rows,
    )
    return result_payload


def _save_query_record(
    store: LocalLogStore,
    payload: dict[str, Any],
) -> None:
    store.save_query_record(
        QueryRecord(
            query_id=payload["query_id"],
            parameters=payload["parameters"],
            row_count=payload["row_count"],
            truncated=payload["truncated"],
            evidence_path=payload["evidence_path"],
            duration_ms=payload["duration_ms"],
            created_at=utc_now(),
            summary=payload["summary"],
        )
    )


def _matching_rows(store: LocalLogStore, query: QueryCriteria) -> list[dict[str, Any]]:
    rows = [row for row in store.list_events() if _matches(row, query)]
    rows.sort(key=lambda row: str(event_time_value(row) or ""))
    return rows


def _query_summary(data: _QuerySummaryInput) -> dict[str, Any]:
    summary = summarize_rows(data.rows, data.parameters)
    summary["returned_row_count"] = data.returned_row_count
    summary["truncated"] = data.truncated
    if data.event_read_audit:
        summary["storage_read_audit"] = data.event_read_audit
        summary["skipped_storage_lines"] = data.event_read_audit.get("skipped_lines", 0)
        summary["corrupt_storage_lines"] = data.event_read_audit.get("corrupt_lines", 0)
    return summary


def _write_query_evidence(
    store: LocalLogStore,
    data: _WriteQueryEvidenceInput,
):
    return LocalEvidenceStore(store.root).write_query_result(
        payload=QueryEvidencePayload(
            query_id=data.query_id,
            parameters=data.parameters,
            rows=data.rows,
            row_count=data.row_count,
            truncated=data.truncated,
            summary=data.summary,
        )
    )


def _query_result(payload: _QueryResultInput) -> QueryResult:
    limited_rows = payload.limited_rows
    return QueryResult(
        query_id=payload.query_id,
        parameters=payload.parameters,
        row_count=payload.row_count,
        truncated=payload.truncated,
        evidence_path=payload.evidence_path,
        evidence_ref=payload.evidence,
        duration_ms=payload.duration_ms,
        summary=payload.summary,
        rows=limited_rows,
        preview_rows=[sanitize_event_for_preview(row) for row in limited_rows[: payload.preview_limit]],
    )


def summarize_rows(rows: list[dict[str, Any]], parameters: dict[str, Any]) -> dict[str, Any]:
    times = [str(event_time_value(row)) for row in rows if event_time_value(row) not in (None, "")]
    return {
        "filters": {key: value for key, value in parameters.items() if key != "limit" and value not in (None, "")},
        "row_count": len(rows),
        "first_event_time": min(times) if times else None,
        "last_event_time": max(times) if times else None,
        "top_alert_types": _top_counts(rows, "alert_type"),
        "top_attackers": _top_counts(rows, "attacker_ip"),
        "top_victims": _top_counts(rows, "victim_ip"),
        "top_domains": _top_counts(rows, "domain"),
    }


def sanitize_event_for_preview(row: dict[str, Any]) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    for field in PREVIEW_FIELDS:
        value = nested_get(row, field)
        if value not in (None, ""):
            preview[field] = value
    payload = nested_get(row, "payload")
    if payload not in (None, ""):
        text = str(payload)
        preview["payload_preview"] = text[:120]
        preview["payload_sha256"] = stable_digest(text)
    return preview


def _criteria(criteria: QueryCriteria | dict[str, Any]) -> QueryCriteria:
    if isinstance(criteria, QueryCriteria):
        return criteria
    return dict_to_model(QueryCriteria, criteria)


def _criteria_to_parameters(criteria: QueryCriteria, limit: int) -> dict[str, Any]:
    data = model_to_dict(criteria)
    data["limit"] = limit
    return data


def _matches(row: dict[str, Any], criteria: QueryCriteria) -> bool:
    if not _matches_time(row, criteria.start_time, criteria.end_time):
        return False
    for field, aliases in FIELD_ALIASES.items():
        expected = getattr(criteria, field)
        if expected in (None, ""):
            continue
        if not _matches_any_alias(row, aliases, str(expected)):
            return False
    return True


def _matches_time(row: dict[str, Any], start_time: str | None, end_time: str | None) -> bool:
    if not start_time and not end_time:
        return True
    value = parse_event_time(event_time_value(row))
    if value is None:
        return False
    start = parse_event_time(start_time) if start_time else None
    end = parse_event_time(end_time) if end_time else None
    if start is not None and value < start:
        return False
    if end is not None and value > end:
        return False
    return True


def _matches_any_alias(row: dict[str, Any], aliases: tuple[str, ...], expected: str) -> bool:
    expected_normalized = expected.lower()
    for alias in aliases:
        actual = nested_get(row, alias)
        if actual is None:
            continue
        if str(actual).lower() == expected_normalized:
            return True
    return False


def _top_counts(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    aliases = FIELD_ALIASES.get(field, (field,))
    for row in rows:
        _count_first_alias(counter, row, aliases)
    return [{"value": value, "count": count} for value, count in counter.most_common(5)]


def _count_first_alias(counter: Counter[str], row: dict[str, Any], aliases: tuple[str, ...]) -> None:
    for alias in aliases:
        value = nested_get(row, alias)
        if value not in (None, ""):
            counter[str(value)] += 1
            return


def _query_id(parameters: dict[str, Any]) -> str:
    stamp = utc_now().replace("-", "").replace(":", "").replace("Z", "")
    payload = {"parameters": parameters, "nonce": time.time_ns()}
    return f"query-{stamp}-{stable_digest(payload)[:10]}"
