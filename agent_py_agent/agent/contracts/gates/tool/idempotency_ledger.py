
 #? 
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ...recovery_actions import RecoveryAction
from ..models import GateDecision
from .manifest import SIDE_EFFECT_TOOL_EFFECTS

_DONE_STATUSES = {"DONE"}
_IN_FLIGHT_STATUSES = {"RUNNING", "PENDING", "WAITING_FOR_TOOL", "DISPATCHED"}


@dataclass(frozen=True)
class IdempotencyLedgerRecord:
    idempotency_key: str
    args_hash: str
    operation_id: str
    status: str
    result_ref: str = ""

@dataclass(frozen=True)
class IdempotencyLedgerFacts:
    tool_name: str
    effect: object
    idempotency_key: str
    args_hash: str
    operation_id: str
    ledger_records: tuple[IdempotencyLedgerRecord | Mapping[str, object], ...] = ()


def evaluate_idempotency_ledger_gate(facts: IdempotencyLedgerFacts) -> GateDecision:
    item = facts
    effect_text = str(item.effect or "").strip().lower()
    if effect_text not in SIDE_EFFECT_TOOL_EFFECTS:
        return GateDecision.allow("idempotency_ledger", evidence={"tool_name": item.tool_name, "effect": effect_text})
    key = str(item.idempotency_key or "").strip()
    if not key:
        return GateDecision.deny("idempotency_ledger", "IDEMPOTENCY_KEY_REQUIRED", evidence={"tool_name": item.tool_name})
    record = _matching_record(key, item.ledger_records)
    if record is None:
        return _allow_new_operation(item, key)
    mismatch = _hash_mismatch_decision(key, item.args_hash, record)
    if mismatch is not None:
        return mismatch
    replay = _replay_decision(item, key, record)
    if replay is not None:
        return replay
    return _allow_new_operation(item, key)


def _matching_record(
    key: str,
    ledger_records: tuple[IdempotencyLedgerRecord | Mapping[str, object], ...],
) -> IdempotencyLedgerRecord | None:
    for record in _ledger_records(ledger_records):
        if record.idempotency_key == key:
            return record
    return None


def _hash_mismatch_decision(key: str, current_hash: str, record: IdempotencyLedgerRecord) -> GateDecision | None:
    current = str(current_hash or "").strip()
    if not (record.args_hash and current and record.args_hash != current):
        return None
    return GateDecision.deny(
        "idempotency_ledger",
        "IDEMPOTENCY_ARGS_HASH_MISMATCH",
        evidence={"idempotency_key": key, "previous_operation_id": record.operation_id},
    )


def _replay_decision(
    facts: IdempotencyLedgerFacts,
    key: str,
    record: IdempotencyLedgerRecord,
) -> GateDecision | None:
    status = record.status.upper()
    if status in _DONE_STATUSES:
        return GateDecision.block(
            "idempotency_ledger",
            "IDEMPOTENCY_REPLAY_REUSE_PREVIOUS_RESULT",
            evidence={
                "idempotency_key": key,
                "previous_operation_id": record.operation_id,
                "result_ref": record.result_ref,
                "operation_id": facts.operation_id,
            },
            recommended_action=RecoveryAction.REUSE_PREVIOUS_RESULT.value,
        )
    if status in _IN_FLIGHT_STATUSES:
        return GateDecision.block(
            "idempotency_ledger",
            "IDEMPOTENCY_OPERATION_IN_FLIGHT",
            evidence={"idempotency_key": key, "previous_operation_id": record.operation_id},
            recommended_action=RecoveryAction.WAIT_FOR_EXISTING_OPERATION.value,
        )
    return None


def _allow_new_operation(facts: IdempotencyLedgerFacts, key: str) -> GateDecision:
    return GateDecision.allow(
        "idempotency_ledger",
        evidence={"tool_name": facts.tool_name, "idempotency_key": key, "operation_id": facts.operation_id},
    )


def _ledger_records(records: tuple[IdempotencyLedgerRecord | Mapping[str, object], ...]) -> list[IdempotencyLedgerRecord]:
    normalized: list[IdempotencyLedgerRecord] = []
    for item in records:
        if isinstance(item, IdempotencyLedgerRecord):
            normalized.append(item)
            continue
        if not isinstance(item, Mapping):
            continue
        normalized.append(
            IdempotencyLedgerRecord(
                idempotency_key=str(item.get("idempotency_key") or ""),
                args_hash=str(item.get("args_hash") or ""),
                operation_id=str(item.get("operation_id") or ""),
                status=str(item.get("status") or ""),
                result_ref=str(item.get("result_ref") or ""),
            )
        )
    return normalized


__all__ = ["IdempotencyLedgerFacts", "IdempotencyLedgerRecord", "evaluate_idempotency_ledger_gate"]
