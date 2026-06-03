
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..contracts.gates import GateDecision, GateFinding, evaluate_fact_evidence_gate
from ..contracts.recovery_actions import RecoveryAction
from .delivery_closeout.quality import (
    delivery_quality_payload_ref,
    workspace_relative_json_path,
)


def fact_evidence_decision(
    *,
    contract: dict[str, Any],
    workspace_root: Path,
    archive_tool_calls: list[dict[str, Any]],
) -> GateDecision:
    fact_contract = fact_evidence_contract(contract)
    if not fact_contract:
        return GateDecision.allow("fact_evidence", evidence={"declared": False})
    payload = load_fact_evidence_payload(contract, workspace_root)
    if payload is None:
        return GateDecision.allow(
            "fact_evidence",
            recommended_action=RecoveryAction.RECORD_FACT_EVIDENCE_PAYLOAD.value,
            evidence={"declared": True, "warning_codes": ["FACT_EVIDENCE_PAYLOAD_MISSING"]},
        )
    decision = evaluate_fact_evidence_gate(payload, fact_contract, archive_tool_calls=archive_tool_calls)
    if not decision.allowed and not _fact_evidence_enforcement_required(fact_contract):
        return GateDecision.allow(
            "fact_evidence",
            recommended_action=RecoveryAction.REVIEW_FACT_EVIDENCE_FINDINGS.value,
            evidence={
                "declared": True,
                "warning_codes": list(decision.finding_codes),
                "advisory_status": decision.status,
                **decision.evidence,
            },
        )
    return decision


def fact_evidence_contract(contract: dict[str, Any]) -> dict[str, Any]:
    value = contract.get("fact_evidence_contract")
    if isinstance(value, dict):
        return dict(value)
    quality = contract.get("delivery_quality_contract")
    if isinstance(quality, dict) and bool(quality.get("require_tool_backed_sources")):
        return dict(quality)
    return {}


def load_fact_evidence_payload(contract: dict[str, Any], workspace_root: Path) -> dict[str, Any] | None:
    ref = fact_evidence_payload_ref(contract)
    if not ref:
        return None
    path = workspace_relative_json_path(ref, workspace_root)
    if path is None or not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, dict) else None


def fact_evidence_payload_ref(contract: dict[str, Any]) -> str:
    for key in ("fact_evidence_payload_ref", "evidence_payload_ref", "source_data_ref"):
        if ref := str(contract.get(key) or "").strip():
            return ref
    return delivery_quality_payload_ref(contract)


def _fact_evidence_enforcement_required(contract: dict[str, Any]) -> bool:
    value = str(contract.get("enforcement") or contract.get("mode") or "").strip().lower()
    return value in {"required", "hard", "block", "blocking"}


__all__ = [
    "fact_evidence_contract",
    "fact_evidence_decision",
    "fact_evidence_payload_ref",
    "load_fact_evidence_payload",
]
