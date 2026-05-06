from __future__ import annotations

"""Compact summaries for parent-session and subagent handoff."""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any

from .contracts import normalize_evidence_refs

CASE_FIELDS = (
    "case_id",
    "id",
    "title",
    "summary",
    "status",
    "priority",
    "severity",
    "risk_score",
    "confidence",
    "created_at",
    "updated_at",
    "time_window",
    "start_time",
    "end_time",
)

ROUTE_FIELDS = (
    "entry_candidates",
    "timeline",
    "impacted_entities",
    "lateral_movement",
    "gaps",
    "next_queries",
    "route_confidence",
)

EVIDENCE_REF_FIELDS = (
    "evidence_ref",
    "evidence_id",
    "ref",
    "id",
    "query_id",
    "path",
    "uri",
    "content_hash",
    "sha256",
    "source",
    "source_id",
    "summary",
    "row_count",
    "truncated",
)


@dataclass
class CaseSummary:
    """Small summary safe for parent prompts."""

    case: dict[str, Any]
    evidence: list[dict[str, Any] | str] = field(default_factory=list)
    route: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": dict(self.case),
            "evidence": list(self.evidence),
            "route": dict(self.route),
        }


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _to_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        payload = value.to_dict()
        return payload if isinstance(payload, Mapping) else {}
    if is_dataclass(value):
        return asdict(value)
    return {}


def _items(source: Any) -> list[Any]:
    if source is None:
        return []
    if isinstance(source, str) or _to_mapping(source):
        return [source]
    try:
        return list(source)
    except TypeError:
        return [source]


def _compact_text(value: Any, *, limit: int = 220) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _compact_list(value: Any, *, limit: int = 8) -> list[Any]:
    output: list[Any] = []
    for item in _items(value)[:limit]:
        _append_compact_item(output, item, limit=limit)
    return output


def _append_compact_item(output: list[Any], item: Any, *, limit: int) -> None:
    compact = _compact_mapping(item, limit=limit) if isinstance(item, Mapping) else _compact_text(item)
    if compact not in (None, ""):
        output.append(compact)


def _compact_mapping(value: Mapping[str, Any], *, limit: int = 8) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in list(value.items())[:limit]:
        if item in (None, "", [], {}):
            continue
        output[str(key)] = _compact_value(item, limit=limit)
    return output


def _compact_value(value: Any, *, limit: int = 8) -> Any:
    if isinstance(value, Mapping):
        return _compact_mapping(value, limit=limit)
    if isinstance(value, list):
        return _compact_list(value, limit=limit)
    return _compact_text(value)


def _summarize_case_fields(case: Any) -> dict[str, Any]:
    output: dict[str, Any] = {}
    case_id = _get(case, "case_id") or _get(case, "id")
    if case_id:
        output["case_id"] = _compact_text(case_id, limit=120)

    for field_name in CASE_FIELDS:
        if field_name in {"case_id", "id"}:
            continue
        value = _get(case, field_name)
        if value in (None, "", [], {}):
            continue
        output[field_name] = _compact_value(value)

    entity_refs = _get(case, "entity_refs") or _get(case, "entities")
    if entity_refs:
        output["entity_refs"] = _compact_list(entity_refs)
    finding_refs = _get(case, "finding_refs") or _get(case, "findings")
    if finding_refs:
        output["finding_refs"] = _compact_list(finding_refs)
    return output


def _summarize_evidence(evidence: Any) -> list[dict[str, Any] | str]:
    output: list[dict[str, Any] | str] = []
    seen: set[str] = set()
    for item in _items(evidence)[:12]:
        _append_evidence_summary(output, seen, item)
    return output


def _append_evidence_summary(output: list[dict[str, Any] | str], seen: set[str], item: Any) -> None:
    mapping = _to_mapping(item)
    if mapping:
        _append_compact_evidence(output, seen, mapping)
        return
    _append_evidence_ref(output, seen, item)


def _append_evidence_ref(output: list[dict[str, Any] | str], seen: set[str], item: Any) -> None:
    refs = normalize_evidence_refs(item, limit=1)
    if refs and refs[0] not in seen:
        output.append(refs[0])
        seen.add(refs[0])


def _append_compact_evidence(output: list[dict[str, Any] | str], seen: set[str], mapping: Mapping[str, Any]) -> None:
    compact = _compact_evidence_mapping(mapping)
    ref_key = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    if compact and ref_key not in seen:
        output.append(compact)
        seen.add(ref_key)


def _compact_evidence_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    compact = {
        field_name: _compact_text(mapping.get(field_name))
        for field_name in EVIDENCE_REF_FIELDS
        if mapping.get(field_name) not in (None, "", [], {})
    }
    metadata = mapping.get("metadata")
    if isinstance(metadata, Mapping):
        _merge_evidence_metadata(compact, metadata)
    return compact


def _merge_evidence_metadata(compact: dict[str, Any], metadata: Mapping[str, Any]) -> None:
    evidence_path = metadata.get("evidence_path") or metadata.get("path")
    if evidence_path and "path" not in compact:
        compact["path"] = _compact_text(evidence_path)
    sha256 = metadata.get("sha256") or metadata.get("content_hash")
    if sha256 and "sha256" not in compact and "content_hash" not in compact:
        compact["sha256"] = _compact_text(sha256)


def _summarize_route(route: Any) -> dict[str, Any]:
    if not route:
        return {}
    output: dict[str, Any] = {}
    for field_name in ROUTE_FIELDS:
        value = _get(route, field_name)
        if value in (None, "", [], {}):
            continue
        output[field_name] = _compact_value(value)
    return output


def summarize_case(case: Any) -> CaseSummary:
    """Build a compact case/evidence/route summary.

    This function intentionally reads only allowlisted summary/ref fields. It
    does not inspect raw_events, raw payloads, query rows, or transcripts.
    """

    evidence = (
        _get(case, "evidence_refs")
        or _get(case, "evidence")
        or _get(case, "evidence_summary")
    )
    route = _get(case, "route_summary") or _get(case, "route_draft") or _get(case, "route")
    return CaseSummary(
        case=_summarize_case_fields(case),
        evidence=_summarize_evidence(evidence),
        route=_summarize_route(route),
    )


def render_case_summary(summary: CaseSummary | Mapping[str, Any]) -> str:
    payload = summary.to_dict() if isinstance(summary, CaseSummary) else dict(summary)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def case_summary_for_prompt(case: Any) -> str:
    return render_case_summary(summarize_case(case))
