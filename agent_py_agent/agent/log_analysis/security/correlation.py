from __future__ import annotations

"""Correlation helpers that turn a case into a route draft."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..models import CaseRecord, EvidenceRef, Finding
from .attack_chain import build_attack_chain, lateral_movement_signs


@dataclass
class RouteDraft:
    case_id: str = ""
    entry_candidates: list[dict[str, Any]] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    impacted_entities: dict[str, list[str]] = field(default_factory=dict)
    lateral_signs: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    inferences: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    next_queries: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_route_draft(
    case: CaseRecord | Mapping[str, Any],
    *,
    findings: Sequence[Finding | Mapping[str, Any]] | None = None,
) -> RouteDraft:
    case_obj = case if isinstance(case, CaseRecord) else CaseRecord.from_dict(case)
    finding_objs = _extract_findings(case_obj, findings)
    impacted_entities = _merge_entities([case_obj.entities, *(finding.entities for finding in finding_objs)])
    facts = [_fact_for_finding(finding) for finding in finding_objs]
    inferences = [_inference_for_finding(finding) for finding in finding_objs]
    entry_candidates = _entry_candidates(finding_objs)
    gaps = _unique([*case_obj.gaps, *(gap for finding in finding_objs for gap in finding.gaps)])
    next_queries = _unique([*case_obj.next_queries, *(query for finding in finding_objs for query in finding.next_queries)])

    if not entry_candidates:
        gaps.append("Entry point is not identified; rank candidate source IP, VPN account, WAF URI, and first host touch.")
        next_queries.append("Build an entry-candidate query across WAF, VPN, SSO, exposed services, and first host activity.")
    if any(finding.detector_id == "waf_attack_success_candidate" for finding in finding_objs) and not any(
        finding.detector_id == "web_to_process_anomaly" for finding in finding_objs
    ):
        gaps.append("WAF path lacks linked EDR process evidence for the victim asset.")
    if any(finding.detector_id in {"vpn_new_geo_login", "bruteforce_then_success"} for finding in finding_objs) and not any(
        "host" in finding.entities or "victim_ip" in finding.entities for finding in finding_objs
    ):
        gaps.append("Identity path lacks linked host logon or asset access evidence.")

    chain = build_attack_chain(finding_objs)
    route = RouteDraft(
        case_id=case_obj.case_id,
        entry_candidates=entry_candidates,
        timeline=[step.to_dict() for step in chain],
        impacted_entities=impacted_entities,
        lateral_signs=lateral_movement_signs(finding_objs),
        facts=facts,
        inferences=inferences,
        gaps=_unique(gaps),
        next_queries=_unique(next_queries),
        evidence_refs=_unique([*_ref_ids(case_obj.evidence_refs), *(ref for finding in finding_objs for ref in _ref_ids(finding.evidence_refs))]),
    )
    attributes = case_obj.attributes if isinstance(case_obj.attributes, Mapping) else {}
    case_obj.attributes = {**attributes, "route_draft": route.to_dict()}
    return route


def draft_route(case: CaseRecord | Mapping[str, Any], *, findings: Sequence[Finding | Mapping[str, Any]] | None = None) -> RouteDraft:
    return build_route_draft(case, findings=findings)


def route_from_case(case: CaseRecord | Mapping[str, Any]) -> RouteDraft:
    return build_route_draft(case)


def _extract_findings(case: CaseRecord, findings: Sequence[Finding | Mapping[str, Any]] | None) -> list[Finding]:
    if findings is not None:
        return _filter_findings_for_case(case, [item if isinstance(item, Finding) else Finding.from_dict(item) for item in findings])
    attributes = case.attributes if isinstance(case.attributes, Mapping) else {}
    return _filter_findings_for_case(
        case,
        [Finding.from_dict(item) for item in attributes.get("finding_summaries", []) if isinstance(item, Mapping)],
    )


def _filter_findings_for_case(case: CaseRecord, findings: Sequence[Finding]) -> list[Finding]:
    refs = {str(ref) for ref in case.finding_refs if str(ref or "").strip()}
    if not refs:
        return list(findings)
    return [finding for finding in findings if finding.finding_id in refs]


def _fact_for_finding(finding: Finding) -> dict[str, Any]:
    return {
        "kind": "detector_finding",
        "detector_id": finding.detector_id,
        "time_window": list(finding.window),
        "statement": f"{finding.detector_id} produced a soft finding with evidence references.",
        "entities": finding.entities,
        "evidence_refs": _ref_ids(finding.evidence_refs),
    }


def _inference_for_finding(finding: Finding) -> dict[str, Any]:
    return {
        "kind": "hypothesis",
        "detector_id": finding.detector_id,
        "hypothesis": finding.hypothesis,
        "confidence": finding.confidence if finding.confidence is not None else finding.risk_score,
        "evidence_refs": _ref_ids(finding.evidence_refs),
    }


def _entry_candidates(findings: Sequence[Finding]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for finding in findings:
        kind = ""
        if finding.detector_id == "waf_attack_success_candidate":
            kind = "web_exploit_candidate"
        elif finding.detector_id == "web_to_process_anomaly":
            kind = "web_post_exploit_execution_candidate"
        elif finding.detector_id == "vpn_new_geo_login":
            kind = "vpn_credential_abuse_candidate"
        elif finding.detector_id == "bruteforce_then_success":
            kind = "bruteforce_credential_abuse_candidate"
        if not kind:
            continue
        candidates.append(
            {
                "kind": kind,
                "detector_id": finding.detector_id,
                "confidence": finding.confidence if finding.confidence is not None else finding.risk_score,
                "entities": finding.entities,
                "evidence_refs": _ref_ids(finding.evidence_refs),
                "basis": "inference",
            }
        )
    return sorted(candidates, key=lambda item: float(item.get("confidence", 0.0)), reverse=True)


def _merge_entities(entity_sets: Sequence[Mapping[str, Sequence[Any]]]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for entities in entity_sets:
        for key, values in entities.items():
            merged.setdefault(str(key), [])
            merged[str(key)].extend(str(value) for value in values)
    return {key: clean for key, values in sorted(merged.items()) if (clean := _unique(values))}


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


__all__ = ["RouteDraft", "build_route_draft", "draft_route", "route_from_case"]
