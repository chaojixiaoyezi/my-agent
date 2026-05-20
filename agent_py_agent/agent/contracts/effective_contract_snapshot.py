# LLM: Effective contract snapshots freeze the exact machine contract used by a run.
# 模块用途: 合并多层结构化合同，保留安全优先级，并生成稳定 hash/ref 校验。

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


# LLM: EffectiveContractSnapshot stores a canonical effective contract and hash.
# 类用途: 返回生效合同、稳定 hash、建议持久化 ref 和逐项 finding。
@dataclass(frozen=True)
class EffectiveContractSnapshot:
    ok: bool
    run_id: str
    effective_contract: dict[str, Any]
    contract_hash: str
    effective_contract_ref: str
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]


# LLM: EffectiveContractValidation reports snapshot/runlog consistency findings.
# 类用途: 返回 run/replay 是否携带匹配的 effective contract ref/hash。
@dataclass(frozen=True)
class EffectiveContractValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]


# LLM: build_effective_contract_snapshot merges explicit contract layers into one canonical contract.
# 函数用途: 生成任务实际使用的合同快照，高层 dangerous_actions 安全字段不能被低层放松。
def build_effective_contract_snapshot(
    *,
    run_id: str,
    layers: tuple[dict[str, Any], ...],
) -> EffectiveContractSnapshot:
    findings: list[dict[str, object]] = []
    effective: dict[str, Any] = {}
    safety: dict[str, dict[str, Any]] = {}
    for layer in layers:
        _merge_layer(effective, safety, layer)
    if safety:
        effective["dangerous_actions"] = safety
    contract_hash = _contract_hash(effective)
    return EffectiveContractSnapshot(
        ok=not findings,
        run_id=run_id,
        effective_contract=effective,
        contract_hash=contract_hash,
        effective_contract_ref=f"runs/{run_id}/effective_contract.json",
        error_codes=(),
        findings=tuple(findings),
    )


# LLM: validate_replay_effective_contract checks replay against the saved contract hash.
# 函数用途: 确认 replay 使用历史 runlog 中记录的 effective_contract_hash。
def validate_replay_effective_contract(
    runlog_entry: dict[str, Any],
    snapshot: EffectiveContractSnapshot,
) -> EffectiveContractValidation:
    expected = _text(runlog_entry.get("contract_hash"))
    if expected == snapshot.contract_hash:
        return _validation(())
    return _validation(("EFFECTIVE_CONTRACT_HASH_MISMATCH",))


# LLM: validate_run_contract_snapshot requires runs to persist a snapshot ref and hash.
# 函数用途: 运行预检时确认 effective_contract_ref 与 contract_hash 都存在。
def validate_run_contract_snapshot(run_record: dict[str, Any]) -> EffectiveContractValidation:
    codes: list[str] = []
    if not _text(run_record.get("effective_contract_ref")):
        codes.append("EFFECTIVE_CONTRACT_REF_MISSING")
    if not _text(run_record.get("contract_hash")):
        codes.append("EFFECTIVE_CONTRACT_HASH_MISSING")
    return _validation(tuple(codes))


# LLM: _merge_layer applies one layer while preserving high-level safety facts.
# 函数用途: 合并普通字段，对 dangerous_actions 使用安全优先合并。
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


# LLM: _merge_dangerous_actions prevents lower layers from relaxing approval/forbidden targets.
# 函数用途: approval_required 只能从 False 升为 True，forbidden_targets 做并集。
def _merge_dangerous_actions(
    current: dict[str, dict[str, Any]],
    incoming: dict[Any, Any],
) -> None:
    for action, policy in incoming.items():
        if not isinstance(policy, dict):
            continue
        action_key = _text(action)
        current[action_key] = _merge_dangerous_policy(dict(current.get(action_key, {})), policy)


# LLM: _merge_dangerous_policy merges one dangerous-action policy with safety precedence.
# 函数用途: 对 approval_required、forbidden_targets 和普通字段分别合并。
def _merge_dangerous_policy(
    current: dict[str, Any],
    incoming: dict[Any, Any],
) -> dict[str, Any]:
    merged = dict(current)
    for key, value in incoming.items():
        _merge_dangerous_field(merged, _text(key), value)
    return merged


# LLM: _merge_dangerous_field applies one safety-aware policy field.
# 函数用途: approval_required 不降级，forbidden_targets 做并集，其余字段深合并。
def _merge_dangerous_field(merged: dict[str, Any], key: str, value: Any) -> None:
    if key == "approval_required":
        merged[key] = bool(merged.get(key)) or value is True
        return
    if key == "forbidden_targets":
        merged[key] = sorted(set(_string_list(merged.get(key))) | set(_string_list(value)))
        return
    merged[key] = _deep_merge(merged.get(key), value)


# LLM: _deep_merge recursively merges dicts and replaces other explicit fields.
# 函数用途: 支撑合同层合并，数组和标量由更低层显式覆盖。
def _deep_merge(left: object, right: object) -> object:
    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            merged[key] = _deep_merge(merged.get(key), value)
        return merged
    return right


# LLM: _contract_hash computes a stable JSON hash for replay and audit.
# 函数用途: 用 sort_keys/minimal separators 生成跨机器稳定 sha256。
def _contract_hash(contract: dict[str, Any]) -> str:
    payload = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# LLM: _validation creates a compact validation report from error codes.
# 函数用途: 统一生成 ok、error_codes 和 finding 列表。
def _validation(codes: tuple[str, ...]) -> EffectiveContractValidation:
    return EffectiveContractValidation(
        ok=not codes,
        error_codes=codes,
        findings=tuple({"code": code} for code in codes),
    )


# LLM: _string_list normalizes explicit string arrays.
# 函数用途: 将 list/tuple/set 规整为去空字符串列表。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value for text in (_text(item),) if text]


# LLM: _text normalizes scalar fields for exact comparisons only.
# 函数用途: 将 None 或标量转成去空白字符串，不解析自然语言语义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = [
    "EffectiveContractSnapshot",
    "EffectiveContractValidation",
    "build_effective_contract_snapshot",
    "validate_replay_effective_contract",
    "validate_run_contract_snapshot",
]
