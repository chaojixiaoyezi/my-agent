from __future__ import annotations

"""Local file content renderers for first response and forensic packages."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .models import CaseRecord, EvidenceRef, Finding, utc_now_iso
from .security.correlation import RouteDraft, build_route_draft


def first_response_report_content(
    case: CaseRecord | Mapping[str, Any],
    route: RouteDraft | Mapping[str, Any] | None = None,
    *,
    findings: Sequence[Finding | Mapping[str, Any]] | None = None,
) -> str:
    case_obj = case if isinstance(case, CaseRecord) else CaseRecord.from_dict(case)
    route_obj = _route_or_build(case_obj, route, findings)
    route_dict = route_obj.to_dict() if isinstance(route_obj, RouteDraft) else dict(route_obj)

    lines = [
        "# First Response Report",
        "",
        f"- Case: {case_obj.case_id}",
        f"- Title: {case_obj.title}",
        f"- Status: {case_obj.status}",
        f"- Priority: {case_obj.priority}",
        f"- Risk score: {case_obj.risk_score:.2f}",
        f"- Generated at: {utc_now_iso()}",
        "",
        "## Facts",
        *_bullet_facts(route_dict.get("facts", [])),
        "",
        "## Inferences",
        *_bullet_inferences(route_dict.get("inferences", [])),
        "",
        "## Entry Candidates",
        *_bullet_entries(route_dict.get("entry_candidates", [])),
        "",
        "## Timeline",
        *_bullet_timeline(route_dict.get("timeline", [])),
        "",
        "## Impacted Entities",
        *_bullet_entities(route_dict.get("impacted_entities", {})),
        "",
        "## Lateral Signs",
        *_bullet_lateral(route_dict.get("lateral_signs", [])),
        "",
        "## Gaps",
        *_bullet_text(route_dict.get("gaps", [])),
        "",
        "## Next Queries",
        *_bullet_query_plans(route_dict.get("next_queries", [])),
        "",
        "## Evidence References",
        *_bullet_text(route_dict.get("evidence_refs", [])),
        "",
    ]
    return "\n".join(lines)


def render_first_response_report(
    case: CaseRecord | Mapping[str, Any],
    route: RouteDraft | Mapping[str, Any] | None = None,
    *,
    findings: Sequence[Finding | Mapping[str, Any]] | None = None,
) -> str:
    return first_response_report_content(case, route, findings=findings)


def build_first_response_report_content(
    case: CaseRecord | Mapping[str, Any],
    route: RouteDraft | Mapping[str, Any] | None = None,
    *,
    findings: Sequence[Finding | Mapping[str, Any]] | None = None,
) -> str:
    return first_response_report_content(case, route, findings=findings)


def build_forensic_package(
    case: CaseRecord | Mapping[str, Any],
    route: RouteDraft | Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    findings = kwargs.get("findings")
    query_history = kwargs.get("query_history")
    sample_rows = kwargs.get("sample_rows")
    raw_refs = kwargs.get("raw_refs")
    frozen = bool(kwargs.get("frozen", False))
    case_obj = case if isinstance(case, CaseRecord) else CaseRecord.from_dict(case)
    route_obj = _route_or_build(case_obj, route, findings)
    route_dict = route_obj.to_dict() if isinstance(route_obj, RouteDraft) else dict(route_obj)
    finding_dicts = _finding_dicts(case_obj, findings)
    evidence_refs = _unique(
        [
            *_ref_ids(case_obj.evidence_refs),
            *route_dict.get("evidence_refs", []),
            *(ref for finding in finding_dicts for ref in _ref_ids(finding.get("evidence_refs", []))),
        ]
    )
    return {
        "package_type": "log_analysis_forensic_package",
        "version": "v1",
        "generated_at": utc_now_iso(),
        "frozen": frozen,
        "case": case_obj.to_dict(),
        "route": route_dict,
        "findings": finding_dicts,
        "facts": route_dict.get("facts", []),
        "inferences": route_dict.get("inferences", []),
        "gaps": route_dict.get("gaps", []),
        "next_queries": route_dict.get("next_queries", []),
        "evidence_refs": evidence_refs,
        "raw_refs": list(raw_refs or _raw_like_refs(case_obj.evidence_refs, evidence_refs)),
        "query_history": [dict(item) for item in query_history or []],
        "sample_rows": [dict(item) for item in sample_rows or []],
        "chain_of_custody": [
            {
                "action": "package_rendered",
                "time": utc_now_iso(),
                "actor": "log_analysis.reports",
                "notes": "Content generated locally; caller owns file write and freeze policy.",
            }
        ],
    }


def forensic_package_content(
    case: CaseRecord | Mapping[str, Any],
    route: RouteDraft | Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> str:
    return json.dumps(
        build_forensic_package(
            case,
            route,
            **kwargs,
        ),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def build_forensic_package_content(
    case: CaseRecord | Mapping[str, Any],
    route: RouteDraft | Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> str:
    return forensic_package_content(case, route, **kwargs)


def _route_or_build(
    case: CaseRecord,
    route: RouteDraft | Mapping[str, Any] | None,
    findings: Sequence[Finding | Mapping[str, Any]] | None,
) -> RouteDraft | Mapping[str, Any]:
    if route is not None:
        return route
    return build_route_draft(case, findings=findings)


def _finding_dicts(case: CaseRecord, findings: Sequence[Finding | Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if findings is None:
        attributes = case.attributes if isinstance(case.attributes, Mapping) else {}
        return _filter_finding_dicts_for_case(case, [dict(item) for item in attributes.get("finding_summaries", []) if isinstance(item, Mapping)])
    return _filter_finding_dicts_for_case(case, [item.to_dict() if isinstance(item, Finding) else dict(item) for item in findings])


def _filter_finding_dicts_for_case(case: CaseRecord, findings: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    refs = {str(ref) for ref in case.finding_refs if str(ref or "").strip()}
    if not refs:
        return list(findings)
    return [finding for finding in findings if str(finding.get("finding_id") or "") in refs]


def _bullet_facts(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No confirmed facts beyond the case shell are available yet."]
    return [
        f"- {item.get('statement', 'Observed fact')} Evidence: {', '.join(item.get('evidence_refs', [])) or 'none'}"
        for item in items
    ]


def _bullet_inferences(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No inferences available yet."]
    return [
        f"- {item.get('hypothesis', 'Hypothesis pending')} Confidence: {float(item.get('confidence', 0.0)):.2f}"
        for item in items
    ]


def _bullet_entries(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No entry candidate is ranked yet."]
    return [
        f"- {item.get('kind', 'candidate')} via {item.get('detector_id', 'unknown')} "
        f"confidence={float(item.get('confidence', 0.0)):.2f} evidence={', '.join(item.get('evidence_refs', [])) or 'none'}"
        for item in items
    ]


def _bullet_timeline(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No timeline steps are available yet."]
    return [
        f"- {item.get('time', 'unknown time')}: {item.get('stage', 'unknown')} - {item.get('action', '')}"
        for item in items
    ]


def _bullet_entities(entities: Mapping[str, Sequence[Any]]) -> list[str]:
    if not entities:
        return ["- No impacted entities are available yet."]
    return [f"- {key}: {', '.join(str(value) for value in values)}" for key, values in sorted(entities.items()) if values]


def _bullet_lateral(items: Sequence[Mapping[str, Any]]) -> list[str]:
    if not items:
        return ["- No lateral movement sign is identified yet."]
    return [f"- {item.get('kind', 'lateral_candidate')}: {item.get('summary', '')}" for item in items]


def _bullet_text(items: Sequence[Any]) -> list[str]:
    values = _unique(items)
    return [f"- {item}" for item in values] if values else ["- None recorded."]


def _bullet_query_plans(items: Sequence[Any]) -> list[str]:
    values = _unique_items(items)
    if not values:
        return ["- None recorded."]
    return [f"- {_format_query_plan(item)}" for item in values]


def _format_query_plan(item: Any) -> str:
    if not isinstance(item, Mapping):
        return str(item)
    display = str(item.get("display") or item.get("purpose") or "Query plan").strip()
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
    return f"{display} ({'; '.join(details)})" if details else display


def _raw_like_refs(refs: Sequence[Any], ref_ids: Sequence[str]) -> list[str]:
    raw: list[str] = []
    for ref in refs:
        if isinstance(ref, EvidenceRef) and ref.raw_ref:
            raw.append(ref.raw_ref)
        elif isinstance(ref, Mapping) and ref.get("raw_ref"):
            raw.append(str(ref.get("raw_ref")))
    raw.extend(ref for ref in ref_ids if ref.startswith("raw") or ":line-" in ref)
    return _unique(raw)


def _ref_ids(refs: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for ref in refs:
        if isinstance(ref, EvidenceRef):
            result.append(ref.evidence_id)
        elif isinstance(ref, Mapping):
            result.append(str(ref.get("evidence_id") or ref.get("raw_ref") or ref))
        else:
            result.append(str(ref))
    return _unique(result)


def _unique(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _unique_items(values: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen or value in ("", None, [], {}):
            continue
        seen.add(marker)
        result.append(value)
    return result


__all__ = [
    "build_first_response_report_content",
    "build_forensic_package",
    "build_forensic_package_content",
    "first_response_report_content",
    "forensic_package_content",
    "render_first_response_report",
]
