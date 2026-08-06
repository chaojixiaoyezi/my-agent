
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..common.value_parsing import string_list, text_value
from .contract_validation_recovery import recovery_for_findings
from .required_actions import RequiredAction


@dataclass(frozen=True)
class EffectiveContractSnapshot:
    ok: bool
    run_id: str
    effective_contract: dict[str, Any]
    contract_hash: str
    effective_contract_ref: str
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None
    required_actions: tuple[RequiredAction, ...] = ()
    required_action_assessment: dict[str, Any] | None = None


@dataclass(frozen=True)
class EffectiveContractValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


def build_effective_contract_snapshot(
    *,
    run_id: str,
    layers: tuple[dict[str, Any], ...],
    required_actions: tuple[RequiredAction, ...] = (),
    required_action_assessment: dict[str, Any] | None = None,
) -> EffectiveContractSnapshot:
    findings: list[dict[str, object]] = []
    effective: dict[str, Any] = {}
    safety: dict[str, dict[str, Any]] = {}
    for layer in layers:
        _merge_layer(effective, safety, layer)
    if safety:
        effective["dangerous_actions"] = safety
    if required_actions:
        effective["required_actions"] = [item.to_dict() for item in required_actions]
    contract_hash = _contract_hash(effective)
    return EffectiveContractSnapshot(
        ok=not findings,
        run_id=run_id,
        effective_contract=effective,
        contract_hash=contract_hash,
        effective_contract_ref=f"runs/{run_id}/effective_contract.json",
        error_codes=(),
        findings=tuple(findings),
        recovery=recovery_for_findings("effective_contract_snapshot", findings),
        required_actions=required_actions,
        required_action_assessment=dict(required_action_assessment or {}),
    )


def validate_replay_effective_contract(
    runlog_entry: dict[str, Any],
    snapshot: EffectiveContractSnapshot,
) -> EffectiveContractValidation:
    expected = text_value(runlog_entry.get("contract_hash"))
    if expected == snapshot.contract_hash:
        return _validation(())
    return _validation(("EFFECTIVE_CONTRACT_HASH_MISMATCH",))


def validate_run_contract_snapshot(run_record: dict[str, Any]) -> EffectiveContractValidation:
    codes: list[str] = []
    if not text_value(run_record.get("effective_contract_ref")):
        codes.append("EFFECTIVE_CONTRACT_REF_MISSING")
    if not text_value(run_record.get("contract_hash")):
        codes.append("EFFECTIVE_CONTRACT_HASH_MISSING")
    return _validation(tuple(codes))


def _merge_layer(
    effective: dict[str, Any],
    safety: dict[str, dict[str, Any]],
    layer: dict[str, Any],
) -> None:
    for key, value in layer.items():
        if key == "layer":
            continue
        if key == "dangerous_actions" and isinstance(value, dict):
            _merge_dangerous_actions(safety, value)
            continue
        effective[key] = _deep_merge(effective.get(key), value)


def _merge_dangerous_actions(
    current: dict[str, dict[str, Any]],
    incoming: dict[Any, Any],
) -> None:
    for action, policy in incoming.items():
        if not isinstance(policy, dict):
            continue
        action_key = text_value(action)
        current[action_key] = _merge_dangerous_policy(dict(current.get(action_key, {})), policy)


def _merge_dangerous_policy(
    current: dict[str, Any],
    incoming: dict[Any, Any],
) -> dict[str, Any]:
    merged = dict(current)
    for key, value in incoming.items():
        _merge_dangerous_field(merged, text_value(key), value)
    return merged


def _merge_dangerous_field(merged: dict[str, Any], key: str, value: Any) -> None:
    if key == "approval_required":
        merged[key] = bool(merged.get(key)) or value is True
        return
    if key == "forbidden_targets":
        merged[key] = sorted(set(string_list(merged.get(key))) | set(string_list(value)))
        return
    merged[key] = _deep_merge(merged.get(key), value)


def _deep_merge(left: object, right: object) -> object:
    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            merged[key] = _deep_merge(merged.get(key), value)
        return merged
    return right


def _contract_hash(contract: dict[str, Any]) -> str:
    payload = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validation(codes: tuple[str, ...]) -> EffectiveContractValidation:
    findings = tuple({"code": code} for code in codes)
    return EffectiveContractValidation(
        ok=not codes,
        error_codes=codes,
        findings=findings,
        recovery=recovery_for_findings("effective_contract_snapshot", findings),
    )


__all__ = [
    "EffectiveContractSnapshot",
    "EffectiveContractValidation",
    "build_effective_contract_snapshot",
    "validate_replay_effective_contract",
    "validate_run_contract_snapshot",
]
