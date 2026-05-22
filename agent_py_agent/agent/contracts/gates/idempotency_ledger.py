# LLM: Idempotency ledger gates make repeated side effects reuse prior results instead of executing twice.
# 模块用途: 根据 idempotency_key、args_hash 和 operation_id 校验副作用去重合同。

 #? 
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .models import GateDecision
from .tool_manifest import SIDE_EFFECT_TOOL_EFFECTS

_COMPLETED_STATUSES = {"SUCCEEDED", "DONE", "COMPLETED", "ALLOW", "OK"}
_IN_FLIGHT_STATUSES = {"RUNNING", "PENDING", "WAITING_FOR_TOOL", "DISPATCHED"}


# LLM: IdempotencyLedgerRecord is a compact record of a prior side-effect operation.
# 类用途: 保存已见幂等键、参数 hash、operation_id 和状态，供执行前阻断重复副作用。
@dataclass(frozen=True)
class IdempotencyLedgerRecord:
    idempotency_key: str
    args_hash: str
    operation_id: str
    status: str
    result_ref: str = ""

# LLM: IdempotencyLedgerFacts is the gate input for one side-effect operation.
# 类用途: 保存当前工具调用和已持久化幂等账本，供执行前判断是否可运行。
@dataclass(frozen=True)
class IdempotencyLedgerFacts:
    tool_name: str
    effect: object
    idempotency_key: str
    args_hash: str
    operation_id: str
    ledger_records: tuple[IdempotencyLedgerRecord | Mapping[str, object], ...] = ()


# LLM: evaluate_idempotency_ledger_gate decides whether a side-effect call is new, duplicate, or mismatched.
# 函数用途: 同 key 同 args 的已完成副作用应复用结果，同 key 不同 args 必须拒绝。
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


# LLM: _matching_record finds an existing operation for one idempotency key.
# 函数用途: 在持久化幂等账本中找同 key 记录，避免主函数累积循环细节。
def _matching_record(
    key: str,
    ledger_records: tuple[IdempotencyLedgerRecord | Mapping[str, object], ...],
) -> IdempotencyLedgerRecord | None:
    for record in _ledger_records(ledger_records):
        if record.idempotency_key == key:
            return record
    return None


# LLM: _hash_mismatch_decision blocks reuse of one idempotency key for different args.
# 函数用途: 同 key 不同 args 说明副作用对象被替换，必须拒绝。
def _hash_mismatch_decision(key: str, current_hash: str, record: IdempotencyLedgerRecord) -> GateDecision | None:
    current = str(current_hash or "").strip()
    if not (record.args_hash and current and record.args_hash != current):
        return None
    return GateDecision.deny(
        "idempotency_ledger",
        "IDEMPOTENCY_ARGS_HASH_MISMATCH",
        evidence={"idempotency_key": key, "previous_operation_id": record.operation_id},
    )


# LLM: _replay_decision turns completed or in-flight duplicate operations into block decisions.
# 函数用途: 同 key 同 args 已完成时复用旧结果，运行中时等待旧操作。
def _replay_decision(
    facts: IdempotencyLedgerFacts,
    key: str,
    record: IdempotencyLedgerRecord,
) -> GateDecision | None:
    status = record.status.upper()
    if status in _COMPLETED_STATUSES:
        return GateDecision.block(
            "idempotency_ledger",
            "IDEMPOTENCY_REPLAY_REUSE_PREVIOUS_RESULT",
            evidence={
                "idempotency_key": key,
                "previous_operation_id": record.operation_id,
                "result_ref": record.result_ref,
                "operation_id": facts.operation_id,
            },
            recommended_action="reuse_previous_result",
        )
    if status in _IN_FLIGHT_STATUSES:
        return GateDecision.block(
            "idempotency_ledger",
            "IDEMPOTENCY_OPERATION_IN_FLIGHT",
            evidence={"idempotency_key": key, "previous_operation_id": record.operation_id},
            recommended_action="wait_for_existing_operation",
        )
    return None


# LLM: _allow_new_operation records that the current idempotency key has no conflicting prior operation.
# 函数用途: 生成新副作用操作可执行的统一 allow 决策。
def _allow_new_operation(facts: IdempotencyLedgerFacts, key: str) -> GateDecision:
    return GateDecision.allow(
        "idempotency_ledger",
        evidence={"tool_name": facts.tool_name, "idempotency_key": key, "operation_id": facts.operation_id},
    )


# LLM: _ledger_records accepts persisted dict rows and dataclass rows with the same semantics.
# 函数用途: 归一化幂等账本记录，缺少关键字段的坏行不会放行或匹配。
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
