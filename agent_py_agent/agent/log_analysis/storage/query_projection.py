
from __future__ import annotations

"""query row matching, summaries, and preview projection for local log storage."""

from collections import Counter
from typing import Any

from .base import QueryCriteria, event_time_value, nested_get, parse_event_time, stable_digest

FIELD_NAME_MAP = {
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


def query_matches(row: dict[str, Any], criteria: QueryCriteria) -> bool:
    if not _matches_time(row, criteria.start_time, criteria.end_time):
        return False
    for field, field_names in FIELD_NAME_MAP.items():
        expected = getattr(criteria, field)
        if expected in (None, ""):
            continue
        if not _matches_any_field_name(row, field_names, str(expected)):
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


def _matches_any_field_name(row: dict[str, Any], field_names: tuple[str, ...], expected: str) -> bool:
    expected_normalized = expected.lower()
    for field_name in field_names:
        actual = nested_get(row, field_name)
        if actual is None:
            continue
        if str(actual).lower() == expected_normalized:
            return True
    return False


def _top_counts(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    field_names = FIELD_NAME_MAP.get(field, (field,))
    for row in rows:
        _count_first_field_name(counter, row, field_names)
    return [{"value": value, "count": count} for value, count in counter.most_common(5)]


def _count_first_field_name(counter: Counter[str], row: dict[str, Any], field_names: tuple[str, ...]) -> None:
    for field_name in field_names:
        value = nested_get(row, field_name)
        if value not in (None, ""):
            counter[str(value)] += 1
            return
