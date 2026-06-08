from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...common.value_parsing import sequence_strings
from ...common.value_parsing import text_value as _text
from ..delivery_quality_checks import (
    delivery_quality_language_findings,
    delivery_quality_metric_findings,
)
from ..evidence_contract import (
    EvidenceClaim,
    EvidenceContractRequest,
    EvidenceSourceRef,
    evaluate_evidence_contract,
)
from ..staged_checkpoint import claims, source_refs
from .models import GateDecision, GateFinding


@dataclass(frozen=True)
class DeliveryQualityTraceScope:
    run_id: str
    task_id: str
    contract_hash: str

def append_delivery_quality_gate_trace(
    trace_path: Path,
    decision: GateDecision,
    scope: DeliveryQualityTraceScope,
) -> None:
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": "delivery_quality_gate",
        "run_id": scope.run_id,
        "task_id": scope.task_id,
        "contract_hash": scope.contract_hash,
        "gate": decision.gate,
        "status": decision.status,
        "allowed": decision.allowed,
        "finding_codes": list(decision.finding_codes),
    }
    with trace_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n")

def evaluate_delivery_quality_gate(
    payload: dict[str, Any],
    contract: dict[str, Any],
    *,
    contract_hash: str = "",
) -> GateDecision:
    source_records = source_refs(payload.get("source_refs"))
    claim_records = claims(payload.get("claims"))
    findings = [
        *_evidence_findings(contract, source_records, claim_records),
        *delivery_quality_metric_findings(contract.get("metric_contracts"), source_records, claim_records),
        *delivery_quality_language_findings(payload, contract.get("language_contract"), claim_records),
        *_artifact_hash_findings(payload, contract_hash),
    ]
    evidence = _decision_evidence(payload, source_records, claim_records, contract_hash)
    if findings:
        return GateDecision.repair("delivery_quality", findings, evidence=evidence)
    return GateDecision.allow("delivery_quality", evidence=evidence)
def _evidence_findings(
    contract: dict[str, Any],
    source_records: list[EvidenceSourceRef],
    claim_records: list[EvidenceClaim],
) -> list[GateFinding]:
    evidence_contract = contract.get("evidence_contract")
    if not isinstance(evidence_contract, dict):
        return []
    report = evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=source_records,
            claims=claim_records,
            required_fields=sequence_strings(evidence_contract.get("required_fields")),
            allowed_value_types=sequence_strings(evidence_contract.get("allowed_value_types")) or ["exact"],
            min_confidence=_float_value(evidence_contract.get("min_confidence")),
            require_methodology_for_estimates=bool(evidence_contract.get("require_methodology_for_estimates", False)),
            require_verified=bool(evidence_contract.get("require_verified", True)),
        )
    )
    return [_gate_finding(item) for item in report.findings]
def _artifact_hash_findings(payload: dict[str, Any], contract_hash: str) -> list[GateFinding]:
    artifacts = _artifact_records(payload)
    if not artifacts or not contract_hash:
        return []
    findings: list[GateFinding] = []
    for index, artifact in enumerate(artifacts):
        artifact_ref = _text(artifact.get("artifact_ref") or artifact.get("path"))
        validated_hash = _text(artifact.get("validated_contract_hash") or artifact.get("contract_hash"))
        findings.extend(_one_artifact_hash_findings(index, artifact_ref, validated_hash, contract_hash))
    return findings
def _one_artifact_hash_findings(
    index: int,
    artifact_ref: str,
    validated_hash: str,
    contract_hash: str,
) -> list[GateFinding]:
    evidence = {"index": index, "artifact_ref": artifact_ref}
    if not validated_hash:
        return [GateFinding("ARTIFACT_VALIDATION_CONTRACT_HASH_MISSING", evidence=evidence)]
    if validated_hash == contract_hash:
        return []
    return [
        GateFinding(
            "ARTIFACT_VALIDATION_CONTRACT_HASH_MISMATCH",
            evidence={
                **evidence,
                "validated_contract_hash": validated_hash,
                "contract_hash": contract_hash,
            },
        )
    ]
def _artifact_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [dict(item) for item in artifacts if isinstance(item, dict)]
def _decision_evidence(
    payload: dict[str, Any],
    source_records: list[EvidenceSourceRef],
    claim_records: list[EvidenceClaim],
    contract_hash: str,
) -> dict[str, Any]:
    return {
        "source_count": len(source_records),
        "claim_count": len(claim_records),
        "artifact_count": len(_artifact_records(payload)),
        "contract_hash": contract_hash,
    }
def _gate_finding(item: dict[str, Any]) -> GateFinding:
    public = {"code", "severity", "message"}
    return GateFinding(
        _text(item.get("code")) or "DELIVERY_QUALITY_FINDING",
        severity=_text(item.get("severity")) or "hard",
        message=_text(item.get("message")),
        evidence={key: value for key, value in item.items() if key not in public},
    )
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
__all__ = [
    "DeliveryQualityTraceScope",
    "append_delivery_quality_gate_trace",
    "delivery_quality_language_findings",
    "delivery_quality_metric_findings",
    "evaluate_delivery_quality_gate",
]
