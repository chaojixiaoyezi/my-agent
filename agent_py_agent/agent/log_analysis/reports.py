
from __future__ import annotations

"""Local file content renderers for first response and forensic packages."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .models import CaseRecord, Finding, utc_now_iso
from .report_formatting import (
    bullet_entities as _bullet_entities,
)
from .report_formatting import (
    bullet_entries as _bullet_entries,
)
from .report_formatting import (
    bullet_facts as _bullet_facts,
)
from .report_formatting import (
    bullet_inferences as _bullet_inferences,
)
from .report_formatting import (
    bullet_lateral as _bullet_lateral,
)
from .report_formatting import (
    bullet_query_plans as _bullet_query_plans,
)
from .report_formatting import (
    bullet_text as _bullet_text,
)
from .report_formatting import (
    bullet_timeline as _bullet_timeline,
)
from .report_formatting import (
    raw_like_refs as _raw_like_refs,
)
from .report_formatting import (
    ref_ids as _ref_ids,
)
from .report_formatting import (
    unique as _unique,
)
from .security.correlation import RouteDraft, build_route_draft


@dataclass(frozen=True)
class ForensicPackageOptions:
    findings: Sequence[Finding | Mapping[str, Any]] | None = None
    query_history: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    sample_rows: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    raw_refs: Sequence[Any] | None = None
    frozen: bool = False


def first_response_report_content(
    case: CaseRecord | Mapping[str, Any],
    route: RouteDraft | Mapping[str, Any] | None = None,
    *,
    findings: Sequence[Finding | Mapping[str, Any]] | None = None,
) -> str:
    case_obj = case if isinstance(case, CaseRecord) else CaseRecord.from_dict(case)
    route_obj = _route_or_build(case_obj, route, findings)
    route_dict = route_obj.to_dict() if isinstance(route_obj, RouteDraft) else dict(route_obj)
    return "\n".join(_first_response_sections(case_obj, route_dict))


def _first_response_sections(case_obj: CaseRecord, route_dict: Mapping[str, Any]) -> list[str]:
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
    return lines


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
    *,
    options: ForensicPackageOptions | None = None,
    findings: Sequence[Finding | Mapping[str, Any]] | None = None,
    query_history: Sequence[Mapping[str, Any]] = (),
    sample_rows: Sequence[Mapping[str, Any]] = (),
    raw_refs: Sequence[Any] | None = None,
    frozen: bool = False,
) -> dict[str, Any]:
    package_options = options or ForensicPackageOptions(
        findings=findings,
        query_history=query_history,
        sample_rows=sample_rows,
        raw_refs=raw_refs,
        frozen=frozen,
    )
    case_obj = case if isinstance(case, CaseRecord) else CaseRecord.from_dict(case)
    route_obj = _route_or_build(case_obj, route, package_options.findings)
    route_dict = _route_dict(route_obj)
    finding_dicts = _finding_dicts(case_obj, package_options.findings)
    evidence_refs = _forensic_evidence_refs(case_obj, route_dict, finding_dicts)
    return {
        "package_type": "log_analysis_forensic_package",
        "version": "v1",
        "generated_at": utc_now_iso(),
        "frozen": package_options.frozen,
        "case": case_obj.to_dict(),
        "route": route_dict,
        "findings": finding_dicts,
        "facts": route_dict.get("facts", []),
        "inferences": route_dict.get("inferences", []),
        "gaps": route_dict.get("gaps", []),
        "next_queries": route_dict.get("next_queries", []),
        "evidence_refs": evidence_refs,
        "raw_refs": list(package_options.raw_refs or _raw_like_refs(case_obj.evidence_refs, evidence_refs)),
        "query_history": [dict(item) for item in package_options.query_history],
        "sample_rows": [dict(item) for item in package_options.sample_rows],
        "chain_of_custody": _chain_of_custody(),
    }


def _chain_of_custody() -> list[dict[str, Any]]:
    return [
        {
            "action": "package_rendered",
            "time": utc_now_iso(),
            "actor": "log_analysis.reports",
            "notes": "Content generated locally; caller owns file write and freeze policy.",
        }
    ]


def _route_dict(route: RouteDraft | Mapping[str, Any]) -> dict[str, Any]:
    return route.to_dict() if isinstance(route, RouteDraft) else dict(route)


def _forensic_evidence_refs(
    case: CaseRecord,
    route: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
) -> list[str]:
    return _unique(
        [
            *_ref_ids(case.evidence_refs),
            *route.get("evidence_refs", []),
            *(ref for finding in findings for ref in _ref_ids(finding.get("evidence_refs", []))),
        ]
    )


def forensic_package_content(
    case: CaseRecord | Mapping[str, Any],
    route: RouteDraft | Mapping[str, Any] | None = None,
    *,
    options: ForensicPackageOptions | None = None,
    findings: Sequence[Finding | Mapping[str, Any]] | None = None,
    query_history: Sequence[Mapping[str, Any]] = (),
    sample_rows: Sequence[Mapping[str, Any]] = (),
    raw_refs: Sequence[Any] | None = None,
    frozen: bool = False,
) -> str:
    return json.dumps(
        build_forensic_package(
            case,
            route,
            options=options,
            findings=findings,
            query_history=query_history,
            sample_rows=sample_rows,
            raw_refs=raw_refs,
            frozen=frozen,
        ),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def build_forensic_package_content(
    case: CaseRecord | Mapping[str, Any],
    route: RouteDraft | Mapping[str, Any] | None = None,
    *,
    options: ForensicPackageOptions | None = None,
    findings: Sequence[Finding | Mapping[str, Any]] | None = None,
    query_history: Sequence[Mapping[str, Any]] = (),
    sample_rows: Sequence[Mapping[str, Any]] = (),
    raw_refs: Sequence[Any] | None = None,
    frozen: bool = False,
) -> str:
    return forensic_package_content(
        case,
        route,
        options=options,
        findings=findings,
        query_history=query_history,
        sample_rows=sample_rows,
        raw_refs=raw_refs,
        frozen=frozen,
    )


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


__all__ = [
    "build_first_response_report_content",
    "build_forensic_package",
    "build_forensic_package_content",
    "first_response_report_content",
    "ForensicPackageOptions",
    "forensic_package_content",
    "render_first_response_report",
]
