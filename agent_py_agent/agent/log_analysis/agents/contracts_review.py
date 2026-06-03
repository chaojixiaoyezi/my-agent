
from __future__ import annotations

"""Reviewer gate for analyst report contracts."""

from collections.abc import Mapping
from typing import Any

from .contracts import (
    AnalystReport,
    ContractValidationError,
    ReviewerDecision,
    _compact_string,
    _get,
    normalize_evidence_refs,
    validate_analyst_report,
)


def review_analyst_report(
    payload: AnalystReport | Mapping[str, Any],
    *,
    known_evidence_refs: list[str] | None = None,
) -> ReviewerDecision:
    """Review an analyst report against evidence boundaries."""

    try:
        report = validate_analyst_report(payload)
    except ContractValidationError as exc:
        return _validation_reject(payload, exc)

    unknown_refs = _unknown_report_refs(report, known_evidence_refs)
    if unknown_refs:
        return _unknown_refs_reject(report, unknown_refs)
    if not report.facts:
        return _missing_facts_decision(report)
    return _approved_decision(report)


def _validation_reject(payload: AnalystReport | Mapping[str, Any], exc: ContractValidationError) -> ReviewerDecision:
    case_id = _compact_string(_get(payload, "case_id") or _get(payload, "id"), limit=120)
    return ReviewerDecision(
        case_id=case_id,
        approved=False,
        decision="REJECT",
        reasons=[str(exc)],
        next_actions=["request_evidence_backed_report"],
    )


def _unknown_report_refs(report: AnalystReport, known_evidence_refs: list[str] | None) -> list[str]:
    known_refs = set(normalize_evidence_refs(known_evidence_refs))
    return [ref for ref in report.evidence_refs if known_refs and ref not in known_refs]


def _unknown_refs_reject(report: AnalystReport, unknown_refs: list[str]) -> ReviewerDecision:
    return ReviewerDecision(
        case_id=report.case_id,
        approved=False,
        decision="REJECT",
        reasons=["report cites evidence_refs outside the reviewer evidence boundary"],
        evidence_refs=list(report.evidence_refs),
        gaps=[f"unknown evidence_ref: {ref}" for ref in unknown_refs],
        next_actions=["rerun analyst with valid evidence refs"],
    )


def _missing_facts_decision(report: AnalystReport) -> ReviewerDecision:
    return ReviewerDecision(
        case_id=report.case_id,
        approved=False,
        decision="NEEDS_MORE_EVIDENCE",
        reasons=["report has evidence_refs but no evidence-backed facts"],
        evidence_refs=list(report.evidence_refs),
        gaps=list(report.gaps) or ["facts missing"],
        next_actions=list(report.next_actions) or ["add facts tied to evidence_refs"],
    )


def _approved_decision(report: AnalystReport) -> ReviewerDecision:
    return ReviewerDecision(
        case_id=report.case_id,
        approved=True,
        decision="APPROVE",
        reasons=["evidence_refs present and facts/inferences/gaps are separated"],
        evidence_refs=list(report.evidence_refs),
        gaps=list(report.gaps),
        next_actions=list(report.next_actions),
    )
