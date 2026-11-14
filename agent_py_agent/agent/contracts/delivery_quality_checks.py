from __future__ import annotations

from typing import Any

from ..common.value_parsing import text_value as _text
from .evidence_contract import EvidenceClaim, EvidenceSourceRef
from .gates.models import GateFinding


def delivery_quality_metric_findings(
    metric_contracts: object,
    source_records: list[EvidenceSourceRef],
    claim_records: list[EvidenceClaim],
) -> list[GateFinding]:
    if not isinstance(metric_contracts, list):
        return []
    sources_by_id = {source.source_id: source for source in source_records if source.source_id}
    findings: list[GateFinding] = []
    for metric_contract in metric_contracts:
        if isinstance(metric_contract, dict):
            findings.extend(_one_metric_contract_findings(metric_contract, claim_records, sources_by_id))
    return findings


def _one_metric_contract_findings(
    metric_contract: dict[str, Any],
    claim_records: list[EvidenceClaim],
    sources_by_id: dict[str, EvidenceSourceRef],
) -> list[GateFinding]:
    field = _text(metric_contract.get("field"))
    expected_kind = _text(metric_contract.get("expected_kind"))
    if not field:
        return [GateFinding("METRIC_CONTRACT_FIELD_MISSING")]
    matched = [claim for claim in claim_records if claim.field == field]
    if not matched:
        return [GateFinding("METRIC_CLAIM_MISSING", evidence={"field": field})]
    findings: list[GateFinding] = []
    for claim in matched:
        findings.extend(_metric_kind_findings(claim, expected_kind, sources_by_id))
        if bool(metric_contract.get("required_window")) and not _has_time_window(claim, sources_by_id):
            findings.append(_claim_metric_finding("METRIC_WINDOW_MISSING", claim, expected_kind=expected_kind))
        findings.extend(_metric_estimate_findings(claim, metric_contract))
    if bool(metric_contract.get("require_consistent_window")):
        findings.extend(_consistent_window_findings(matched, sources_by_id))
    return findings


def _metric_kind_findings(
    claim: EvidenceClaim,
    expected_kind: str,
    sources_by_id: dict[str, EvidenceSourceRef],
) -> list[GateFinding]:
    if not expected_kind:
        return []
    actual_kind = _metric_kind(claim, sources_by_id)
    if not actual_kind:
        return [_claim_metric_finding("METRIC_KIND_MISSING", claim, expected_kind=expected_kind)]
    if actual_kind == expected_kind:
        return []
    return [_claim_metric_finding("METRIC_KIND_MISMATCH", claim, expected_kind=expected_kind, actual_kind=actual_kind)]


def _metric_estimate_findings(claim: EvidenceClaim, metric_contract: dict[str, Any]) -> list[GateFinding]:
    if _text(claim.value_type) != "estimated":
        return []
    findings: list[GateFinding] = []
    if metric_contract.get("allow_estimated") is False:
        findings.append(_claim_metric_finding("METRIC_ESTIMATE_NOT_ALLOWED", claim))
    if bool(metric_contract.get("require_limitations_for_estimates")) and not _has_estimate_limitations(claim):
        findings.append(_claim_metric_finding("METRIC_ESTIMATE_LIMITATIONS_MISSING", claim))
    return findings


def _metric_kind(claim: EvidenceClaim, sources_by_id: dict[str, EvidenceSourceRef]) -> str:
    for value in (claim.metric_kind, claim.observed_metric_kind):
        if kind := _text(value):
            return kind
    for source_id in claim.source_ids:
        source = sources_by_id.get(source_id)
        if source and (kind := _text(source.metric_kind)):
            return kind
    return ""


def _has_time_window(claim: EvidenceClaim, sources_by_id: dict[str, EvidenceSourceRef]) -> bool:
    if _claim_time_window(claim) is not None:
        return True
    return any(_source_time_window(sources_by_id[source_id]) for source_id in claim.source_ids if source_id in sources_by_id)


def _consistent_window_findings(
    claims: list[EvidenceClaim],
    sources_by_id: dict[str, EvidenceSourceRef],
) -> list[GateFinding]:
    windows = {window for claim in claims if (window := _time_window(claim, sources_by_id))}
    if len(windows) <= 1:
        return []
    return [
        _claim_metric_finding(
            "METRIC_WINDOW_INCONSISTENT",
            claims[0],
            actual_kind=",".join(sorted(f"{start}..{end}" for start, end in windows)[:6]),
        )
    ]


def _time_window(claim: EvidenceClaim, sources_by_id: dict[str, EvidenceSourceRef]) -> tuple[str, str] | None:
    if window := _claim_time_window(claim):
        return window
    for source_id in claim.source_ids:
        source = sources_by_id.get(source_id)
        if source and (window := _source_time_window(source)):
            return window
    return None


def _claim_time_window(claim: EvidenceClaim) -> tuple[str, str] | None:
    return _time_window_fields(claim.window_start, claim.window_end, claim.time_window)


def _source_time_window(source: EvidenceSourceRef) -> tuple[str, str] | None:
    return _time_window_fields(source.window_start, source.window_end, source.time_window)


def _time_window_fields(start: object, end: object, window: object) -> tuple[str, str] | None:
    if _text(start) and _text(end):
        return (_text(start), _text(end))
    if isinstance(window, dict) and _text(window.get("start")) and _text(window.get("end")):
        return (_text(window.get("start")), _text(window.get("end")))
    return None


def _has_estimate_limitations(claim: EvidenceClaim) -> bool:
    for value in (claim.limitations, claim.uncertainty_notes):
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and any(_text(item) for item in value):
            return True
    return False


def _claim_metric_finding(
    code: str,
    claim: EvidenceClaim,
    *,
    expected_kind: str = "",
    actual_kind: str = "",
) -> GateFinding:
    evidence = {"claim_id": claim.claim_id, "field": claim.field}
    if expected_kind:
        evidence["expected_kind"] = expected_kind
    if actual_kind:
        evidence["actual_kind"] = actual_kind
    return GateFinding(code, severity="advisory", evidence=evidence)
