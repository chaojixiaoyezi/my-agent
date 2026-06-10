
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..common.value_parsing import sequence_strings
from .contract_validation_recovery import recovery_for_findings


@dataclass(frozen=True)
class EvidenceSourceRef:
    source_id: str
    source_type: str = ""
    uri: str = ""
    retrieved_at: str = ""
    artifact_ref: str = ""
    content_sha256: str = ""
    status: str = "AVAILABLE"
    tool_call_ref: str = ""
    tool_call_id: str = ""
    operation_id: str = ""
    tool_result_id: str = ""
    http_status: int | None = None
    metric_kind: str = ""
    window_start: str = ""
    window_end: str = ""
    time_window: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "uri": self.uri,
            "retrieved_at": self.retrieved_at,
            "artifact_ref": self.artifact_ref,
            "content_sha256": self.content_sha256,
            "status": self.status,
        }
        _add_present(
            payload,
            {
                "tool_call_ref": self.tool_call_ref,
                "tool_call_id": self.tool_call_id,
                "operation_id": self.operation_id,
                "tool_result_id": self.tool_result_id,
                "http_status": self.http_status,
                "metric_kind": self.metric_kind,
                "window_start": self.window_start,
                "window_end": self.window_end,
                "time_window": self.time_window,
            },
        )
        return payload


@dataclass(frozen=True)
class EvidenceClaim:
    claim_id: str
    field: str
    value: Any
    source_ids: list[str] = field(default_factory=list)
    confidence: float = 1.0
    verification_status: str = "VERIFIED"
    value_type: str = "exact"
    methodology: str = ""
    metric_kind: str = ""
    observed_metric_kind: str = ""
    window_start: str = ""
    window_end: str = ""
    time_window: dict[str, Any] = field(default_factory=dict)
    limitations: str | list[str] = ""
    uncertainty_notes: str | list[str] = ""
    item_path: str = ""
    item_key: dict[str, Any] = field(default_factory=dict)
    item_index: int | None = None
    group_index: int | None = None
    group_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "claim_id": self.claim_id,
            "field": self.field,
            "value": self.value,
            "source_ids": list(self.source_ids),
            "confidence": self.confidence,
            "verification_status": self.verification_status,
            "value_type": self.value_type,
            "methodology": self.methodology,
        }
        _add_present(
            payload,
            {
                "metric_kind": self.metric_kind,
                "observed_metric_kind": self.observed_metric_kind,
                "window_start": self.window_start,
                "window_end": self.window_end,
                "time_window": self.time_window,
                "limitations": self.limitations,
                "uncertainty_notes": self.uncertainty_notes,
                "item_path": self.item_path,
                "item_key": self.item_key,
                "item_index": self.item_index,
                "group_index": self.group_index,
                "group_name": self.group_name,
            },
        )
        return payload


@dataclass(frozen=True)
class EvidenceContractRequest:
    source_refs: list[EvidenceSourceRef] = field(default_factory=list)
    claims: list[EvidenceClaim] = field(default_factory=list)
    required_fields: list[str] = field(default_factory=list)
    require_verified: bool = False
    allowed_value_types: list[str] = field(default_factory=lambda: ["exact"])
    min_confidence: float = 0.0
    require_methodology_for_estimates: bool = False


@dataclass(frozen=True)
class EvidenceContractReport:
    ok: bool
    summary: dict[str, int]
    findings: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[EvidenceSourceRef] = field(default_factory=list)
    claims: list[EvidenceClaim] = field(default_factory=list)
    recovery: dict[str, object] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "summary": dict(self.summary),
            "findings": list(self.findings),
            "source_refs": [item.to_dict() for item in self.source_refs],
            "claims": [item.to_dict() for item in self.claims],
        }
        recovery = self.recovery or recovery_for_findings("evidence_contract", self.findings)
        if recovery is not None:
            payload["recovery"] = recovery
        return payload


@dataclass(frozen=True)
class _ClaimValidationContext:
    sources_by_id: dict[str, EvidenceSourceRef]
    allowed_value_types: set[str]
    min_confidence: float
    require_methodology_for_estimates: bool
    require_verified: bool


def evaluate_evidence_contract(request: EvidenceContractRequest) -> EvidenceContractReport:
    sources_by_id = {item.source_id: item for item in request.source_refs if item.source_id}
    findings: list[dict[str, Any]] = []
    findings.extend(_source_findings(request.source_refs))
    findings.extend(_claim_findings(request.claims, _claim_validation_context(request, sources_by_id)))
    findings.extend(_required_field_findings(request.required_fields, request.claims))
    return EvidenceContractReport(
        ok=not findings,
        summary=_summary(request, findings),
        findings=findings,
        source_refs=list(request.source_refs),
        claims=list(request.claims),
        recovery=recovery_for_findings("evidence_contract", findings),
    )


def _source_findings(source_refs: list[EvidenceSourceRef]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for item in source_refs:
        if not item.source_id or not (item.uri or item.artifact_ref):
            findings.append(
                _finding(
                    "EVIDENCE_SOURCE_UNREADABLE",
                    "hard",
                    "source ref lacks source_id or readable uri/artifact_ref",
                    details={"source_id": item.source_id},
                )
            )
    return findings


def _claim_findings(claims: list[EvidenceClaim], context: _ClaimValidationContext) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for claim in claims:
        if not claim.source_ids:
            findings.append(_claim_finding("EVIDENCE_CLAIM_UNSOURCED", "claim has no source ids", claim))
            continue
        missing = [source_id for source_id in claim.source_ids if source_id not in context.sources_by_id]
        if missing:
            findings.append(_claim_finding("EVIDENCE_SOURCE_MISSING", "claim references missing source ids", claim, {"missing_source_ids": missing}))
        findings.extend(_claim_policy_findings(claim, context))
    return findings


def _claim_validation_context(
    request: EvidenceContractRequest,
    sources_by_id: dict[str, EvidenceSourceRef],
) -> _ClaimValidationContext:
    allowed_types = {str(item).strip() for item in request.allowed_value_types if str(item).strip()} or {"exact"}
    return _ClaimValidationContext(
        sources_by_id=sources_by_id,
        allowed_value_types=allowed_types,
        min_confidence=request.min_confidence,
        require_methodology_for_estimates=request.require_methodology_for_estimates,
        require_verified=request.require_verified,
    )


def _claim_policy_findings(claim: EvidenceClaim, context: _ClaimValidationContext) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if context.require_verified and claim.verification_status != "VERIFIED":
        findings.append(_claim_finding("EVIDENCE_CLAIM_UNVERIFIED", "claim verification_status is not VERIFIED", claim))
    findings.extend(_claim_value_type_findings(claim, context))
    findings.extend(_claim_confidence_findings(claim, context))
    findings.extend(_claim_estimate_method_findings(claim, context))
    return findings


def _claim_value_type_findings(claim: EvidenceClaim, context: _ClaimValidationContext) -> list[dict[str, Any]]:
    value_type = str(claim.value_type or "exact").strip()
    if value_type in context.allowed_value_types:
        return []
    return [
        _claim_finding(
            "EVIDENCE_VALUE_TYPE_NOT_ALLOWED",
            "claim value_type is not allowed by the evidence contract",
            claim,
            {"value_type": value_type},
        )
    ]


def _claim_confidence_findings(claim: EvidenceClaim, context: _ClaimValidationContext) -> list[dict[str, Any]]:
    if context.min_confidence <= 0 or claim.confidence >= context.min_confidence:
        return []
    return [
        _claim_finding(
            "EVIDENCE_CLAIM_LOW_CONFIDENCE",
            "claim confidence is below the required minimum",
            claim,
            {"confidence": claim.confidence, "min_confidence": context.min_confidence},
        )
    ]


def _claim_estimate_method_findings(claim: EvidenceClaim, context: _ClaimValidationContext) -> list[dict[str, Any]]:
    value_type = str(claim.value_type or "exact").strip()
    if not context.require_methodology_for_estimates or value_type != "estimated" or claim.methodology.strip():
        return []
    return [_claim_finding("EVIDENCE_ESTIMATE_METHOD_MISSING", "estimated claim lacks machine-readable methodology", claim)]


def _claim_finding(code: str, message: str, claim: EvidenceClaim, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return _finding(code, "hard", message, details={"claim_id": claim.claim_id, **(details or {})})


def _required_field_findings(required_fields: list[str], claims: list[EvidenceClaim]) -> list[dict[str, Any]]:
    claimed_fields = {item.field for item in claims if item.field}
    return [
        _finding(
            "EVIDENCE_REQUIRED_FIELD_MISSING",
            "hard",
            "required field has no claim",
            details={"field": field},
        )
        for field in sorted({item for item in required_fields if item and item not in claimed_fields})
    ]


def _summary(request: EvidenceContractRequest, findings: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "sources": len(request.source_refs),
        "claims": len(request.claims),
        "verified_claims": sum(item.verification_status == "VERIFIED" for item in request.claims),
        "findings": len(findings),
    }


def _finding(code: str, severity: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"code": code, "severity": severity, "message": message, **dict(details or {})}


def _add_present(payload: dict[str, Any], values: dict[str, Any]) -> None:
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        if isinstance(value, dict) and not value:
            continue
        if isinstance(value, list) and not value:
            continue
        payload[key] = value


__all__ = [
    "EvidenceClaim",
    "EvidenceContractReport",
    "EvidenceContractRequest",
    "EvidenceSourceRef",
    "evaluate_evidence_contract",
]


# ---- 证据记录归一化（原 staged_checkpoint.py 并入，types 与 normalizer 同一权威位置） ----
def source_refs(value: object) -> list[EvidenceSourceRef]:
    if not isinstance(value, list):
        return []
    refs: list[EvidenceSourceRef] = []
    for item in value:
        if isinstance(item, dict):
            refs.append(_source_ref(item))
    return refs


def claims(value: object) -> list[EvidenceClaim]:
    if not isinstance(value, list):
        return []
    return [_claim(index, item) for index, item in enumerate(value) if isinstance(item, dict)]


def _source_ref(item: dict[str, Any]) -> EvidenceSourceRef:
    return EvidenceSourceRef(
        source_id=str(item.get("source_id") or ""),
        source_type=str(item.get("source_type") or ""),
        uri=str(item.get("uri") or ""),
        retrieved_at=str(item.get("retrieved_at") or ""),
        artifact_ref=str(item.get("artifact_ref") or ""),
        content_sha256=str(item.get("content_sha256") or ""),
        status=str(item.get("status") or "AVAILABLE"),
        tool_call_ref=str(item.get("tool_call_ref") or ""),
        tool_call_id=str(item.get("tool_call_id") or ""),
        operation_id=str(item.get("operation_id") or ""),
        tool_result_id=str(item.get("tool_result_id") or ""),
        http_status=_optional_int(item.get("http_status")),
        metric_kind=str(item.get("metric_kind") or ""),
        window_start=str(item.get("window_start") or ""),
        window_end=str(item.get("window_end") or ""),
        time_window=dict(item.get("time_window")) if isinstance(item.get("time_window"), dict) else {},
    )


def _claim(index: int, item: dict[str, Any]) -> EvidenceClaim:
    return EvidenceClaim(
        claim_id=str(item.get("claim_id") or f"claim-{index}"),
        field=str(item.get("field") or ""),
        value=item.get("value"),
        source_ids=sequence_strings(item.get("source_ids")),
        confidence=float(item.get("confidence", 1.0) or 0.0),
        verification_status=str(item.get("verification_status") or "VERIFIED"),
        value_type=str(item.get("value_type") or "exact"),
        methodology=str(item.get("methodology") or ""),
        metric_kind=str(item.get("metric_kind") or ""),
        observed_metric_kind=str(item.get("observed_metric_kind") or ""),
        window_start=str(item.get("window_start") or ""),
        window_end=str(item.get("window_end") or ""),
        time_window=dict(item.get("time_window")) if isinstance(item.get("time_window"), dict) else {},
        limitations=item.get("limitations") or "",
        uncertainty_notes=item.get("uncertainty_notes") or "",
        item_path=str(item.get("item_path") or ""),
        item_key=dict(item.get("item_key")) if isinstance(item.get("item_key"), dict) else {},
        item_index=_optional_int(item.get("item_index")),
        group_index=_optional_int(item.get("group_index")),
        group_name=str(item.get("group_name") or ""),
    )


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
