from __future__ import annotations

"""Formatting helpers for log-analysis report renderers."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .models import EvidenceRef, QueryPlan


def bullet_facts(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No confirmed facts beyond the case shell are available yet."]
    return [
        f"- {item.get('statement', 'Observed fact')} Evidence: {', '.join(item.get('evidence_refs', [])) or 'none'}"
        for item in items
    ]


def bullet_inferences(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No inferences available yet."]
    return [
        f"- {item.get('hypothesis', 'Hypothesis pending')} Confidence: {float(item.get('confidence', 0.0)):.2f}"
        for item in items
    ]


def bullet_entries(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No entry candidate is ranked yet."]
    return [
        f"- {item.get('kind', 'candidate')} via {item.get('detector_id', 'unknown')} "
        f"confidence={float(item.get('confidence', 0.0)):.2f} evidence={', '.join(item.get('evidence_refs', [])) or 'none'}"
        for item in items
    ]


def bullet_timeline(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No timeline steps are available yet."]
    return [
        f"- {item.get('time', 'unknown time')}: {item.get('stage', 'unknown')} - {item.get('action', '')}"
        for item in items
    ]


def bullet_entities(entities: Mapping[str, Sequence[Any]]) -> list[str]:
    if not entities:
        return ["- No impacted entities are available yet."]
    return [f"- {key}: {', '.join(str(value) for value in values)}" for key, values in sorted(entities.items()) if values]


def bullet_lateral(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No lateral movement sign is identified yet."]
    return [f"- {item.get('kind', 'lateral_candidate')}: {item.get('summary', '')}" for item in items]


def bullet_text(items: Sequence[Any]) -> list[str]:
    values = unique(items)
    return [f"- {item}" for item in values] if values else ["- None recorded."]


def bullet_query_plans(items: Sequence[Any]) -> list[str]:
    values = unique_items(items)
    if not values:
        return ["- None recorded."]
    return [f"- {format_query_plan(item)}" for item in values]


def format_query_plan(item: Any) -> str:
    if not isinstance(item, Mapping):
        return str(item)
    display = str(item.get("display") or item.get("purpose") or "Query plan").strip()
    details = query_plan_details(item)
    return f"{display} ({'; '.join(details)})" if details else display


def query_plan_details(item: Mapping[str, Any]) -> list[str]:
    details: list[str] = []
    source_products = item.get("source_products") or []
    if source_products:
        details.append(f"sources={', '.join(str(value) for value in source_products)}")
    start_time = str(item.get("start_time") or "").strip()
    end_time = str(item.get("end_time") or "").strip()
    if start_time or end_time:
        details.append(f"time={start_time or '*'}..{end_time or '*'}")
    filters = item.get("filters")
    if isinstance(filters, Mapping) and filters:
        rendered = ", ".join(f"{key}={value}" for key, value in sorted(filters.items()) if value not in (None, "", [], {}))
        if rendered:
            details.append(f"filters: {rendered}")
    evidence_needed = item.get("evidence_needed") or []
    if evidence_needed:
        details.append(f"evidence={', '.join(str(value) for value in evidence_needed)}")
    limit = item.get("limit")
    if limit:
        details.append(f"limit={limit}")
    return details


def raw_like_refs(refs: Sequence[Any], ref_ids: Sequence[str]) -> list[str]:
    raw: list[str] = []
    for ref in refs:
        _append_raw_ref(raw, ref)
    raw.extend(ref for ref in ref_ids if ref.startswith("raw") or ":line-" in ref)
    return unique(raw)


def _append_raw_ref(raw: list[str], ref: Any) -> None:
    if isinstance(ref, EvidenceRef) and ref.raw_ref:
        raw.append(ref.raw_ref)
        return
    if isinstance(ref, Mapping) and ref.get("raw_ref"):
        raw.append(str(ref.get("raw_ref")))


def ref_ids(refs: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for ref in refs:
        result.append(ref_id(ref))
    return unique(result)


def ref_id(ref: Any) -> str:
    if isinstance(ref, EvidenceRef):
        return ref.evidence_id
    if isinstance(ref, Mapping):
        return str(ref.get("evidence_id") or ref.get("raw_ref") or ref)
    return str(ref)


def unique(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def unique_items(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        item: Any = value.to_dict() if isinstance(value, QueryPlan) else value
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen or item in ("", None, [], {}):
            continue
        seen.add(marker)
        result.append(item)
    return result
