
from __future__ import annotations

from typing import Any

from ...contracts.error_taxonomy import error_contract
from ...contracts.staged_checkpoint_acceptance import (
    StagedEvidenceRequest,
    staged_json_evidence_findings,
)
from .recovery_models import (
    RecoveryActionLedger,
    StagedEvidenceActionRequest,
)


def append_staged_evidence_actions(request: StagedEvidenceActionRequest) -> bool:
    evidence_contract = request.validation_contract.get("evidence_contract")
    if not isinstance(evidence_contract, dict):
        return False
    findings = staged_json_evidence_findings(
        StagedEvidenceRequest(request.checkpoint_ref, request.workspace_root, evidence_contract)
    )
    for action in _evidence_actions(request.checkpoint_ref, findings):
        _append_evidence_action(request.ledger, action)
    return bool(findings)


def _append_evidence_action(ledger: RecoveryActionLedger, action: dict[str, object]) -> None:
    action_code = str(action.get("code") or "")
    if not action_code or action_code in ledger.seen:
        return
    ledger.seen.add(action_code)
    ledger.actions.append(action)


def _evidence_actions(checkpoint_ref: str, findings: list[dict[str, Any]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        code = str(finding.get("code") or "")
        if code:
            grouped.setdefault(code, []).append(finding)
    return [_one_grouped_evidence_action(checkpoint_ref, code, items) for code, items in grouped.items()]


def _one_grouped_evidence_action(
    checkpoint_ref: str,
    action_code: str,
    findings: list[dict[str, Any]],
) -> dict[str, object]:
    contract = error_contract(action_code)
    action: dict[str, object] = {
        "code": contract.code,
        "category": contract.category,
        "retryable": contract.retryable,
        "recommended_action": contract.recommended_action,
        "recovery_hint": contract.recovery_hint,
        "checkpoint_ref": checkpoint_ref,
    }
    fields = sorted({field for item in findings if (field := str(item.get("field") or "").strip())})
    claim_ids = sorted({claim_id for item in findings if (claim_id := str(item.get("claim_id") or "").strip())})
    if fields:
        action["required_fields"] = fields
    if claim_ids:
        action["claim_ids"] = claim_ids
    action.update(_evidence_writer_fields(checkpoint_ref))
    return action


def _evidence_writer_fields(checkpoint_ref: str) -> dict[str, object]:
    if not checkpoint_ref.lower().endswith(".json"):
        return {}
    return {
        "writer_tool": "write_file",
        "write_tools": ["write_file"],
        "evidence_shape_hint": (
            '{"source_refs":[{"source_id":"src-1","uri":"https://...","retrieved_at":"..."}],'
            '"claims":[{"field":"...","value":"...","source_ids":["src-1"],'
            '"verification_status":"VERIFIED","value_type":"exact","confidence":1.0,'
            '"methodology":"how this value was obtained"}]}'
        ),
    }


__all__ = ["append_staged_evidence_actions"]
