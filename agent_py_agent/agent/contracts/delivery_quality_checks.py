from __future__ import annotations

from typing import Any

from ..common.value_parsing import sequence_strings
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
    for key in ("metric_kind", "observed_metric_kind"):
        if kind := _text(claim.reserved.get(key)):
            return kind
    for source_id in claim.source_ids:
        source = sources_by_id.get(source_id)
        if source and (kind := _text(source.reserved.get("metric_kind"))):
            return kind
    return ""


def _has_time_window(claim: EvidenceClaim, sources_by_id: dict[str, EvidenceSourceRef]) -> bool:
    if _time_window_fields(claim.reserved) is not None:
        return True
    return any(_time_window_fields(sources_by_id[source_id].reserved) for source_id in claim.source_ids if source_id in sources_by_id)


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
    if window := _time_window_fields(claim.reserved):
        return window
    for source_id in claim.source_ids:
        source = sources_by_id.get(source_id)
        if source and (window := _time_window_fields(source.reserved)):
            return window
    return None


def _time_window_fields(value: dict[str, Any]) -> tuple[str, str] | None:
    if _text(value.get("window_start")) and _text(value.get("window_end")):
        return (_text(value.get("window_start")), _text(value.get("window_end")))
    window = value.get("time_window")
    if isinstance(window, dict) and _text(window.get("start")) and _text(window.get("end")):
        return (_text(window.get("start")), _text(window.get("end")))
    return None


def _has_estimate_limitations(claim: EvidenceClaim) -> bool:
    for key in ("limitations", "uncertainty_notes"):
        value = claim.reserved.get(key)
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


def delivery_quality_language_findings(
    payload: dict[str, Any],
    language_contract: object,
    claim_records: list[EvidenceClaim],
) -> list[GateFinding]:
    if not isinstance(language_contract, dict):
        return []
    target = _text(language_contract.get("target_language"))
    fields = sequence_strings(language_contract.get("fields"))
    if target != "zh" or not fields:
        return []
    min_cjk = _int_value(language_contract.get("min_cjk_chars"), default=4)
    max_latin_ratio = _float_value(language_contract.get("max_latin_ratio"), default=0.45)
    return [
        _language_finding(field, value, location, target)
        for field, value, location in _language_candidates(payload, claim_records, fields)
        if not _looks_like_zh(value, min_cjk=min_cjk, max_latin_ratio=max_latin_ratio)
    ]


def _language_candidates(
    payload: dict[str, Any],
    claim_records: list[EvidenceClaim],
    fields: list[str],
) -> list[tuple[str, str, str]]:
    rows = _row_records(payload)
    if rows:
        return [(field, str(row.get(field) or ""), f"items[{index}].{field}") for index, row in enumerate(rows) for field in fields if field in row]
    return [(claim.field, str(claim.value or ""), f"claims.{claim.claim_id}") for claim in claim_records if claim.field in fields]


def _row_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if rows := _list_of_dicts(payload.get("items")):
        return rows
    if rows := _list_of_dicts(payload.get("rows")):
        return rows
    data = payload.get("data")
    if isinstance(data, dict) and (rows := _list_of_dicts(data.get("rows"))):
        return rows
    return _sheet_rows(payload.get("sheets"))


def _sheet_rows(value: object) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return rows
    for sheet in value:
        if isinstance(sheet, dict):
            rows.extend(_list_of_dicts(sheet.get("rows")))
    return rows


def _list_of_dicts(value: object) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(value, list) and isinstance(item, dict)]


def _language_finding(field: str, value: str, location: str, target: str) -> GateFinding:
    return GateFinding(
        "LANGUAGE_FIELD_TARGET_MISMATCH",
        evidence={"field": field, "location": location, "target_language": target, "cjk_chars": _cjk_count(value), "latin_ratio": _latin_ratio(value)},
    )


def _looks_like_zh(value: str, *, min_cjk: int, max_latin_ratio: float) -> bool:
    text = value.strip()
    return bool(text) and _cjk_count(text) >= min_cjk and _latin_ratio(text) <= max_latin_ratio


def _cjk_count(value: str) -> int:
    return sum(1 for char in value if "\u4e00" <= char <= "\u9fff")


def _latin_ratio(value: str) -> float:
    letters = [char for char in value if char.isalpha()]
    if not letters:
        return 0.0
    return len([char for char in letters if "a" <= char.lower() <= "z"]) / len(letters)


def _float_value(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_value(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
