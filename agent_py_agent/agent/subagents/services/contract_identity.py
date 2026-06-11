
from __future__ import annotations

"""子代理合约身份计算：从 context packs 提取幂等/修复合约的稳定身份元组。

幂等合约和修复合约共用同一套 pack 遍历与字符串归一化 helper；两类合约各自的
身份字段不同，分别由 idempotency / repair 两个入口函数计算。
"""

from typing import Any

from ...common.value_parsing import text_value

_IDEMPOTENCY_CONTRACT_SCHEMA = "subagent_idempotency_contract.v1"
_REPAIR_CONTRACT_SCHEMA = "subagent_repair_contract.v1"


def idempotency_contract_identity_from_context_packs(value: object) -> tuple[object, ...]:
    for pack in _iter_packs(value):
        contract = pack.get("contract")
        if text_value(contract.get("schema") if isinstance(contract, dict) else "") == _IDEMPOTENCY_CONTRACT_SCHEMA:
            return _idempotency_identity(contract)
    return ()


def repair_contract_identity_from_context_packs(value: object) -> tuple[object, ...]:
    for pack in _iter_packs(value):
        contract = pack.get("contract")
        if text_value(contract.get("schema") if isinstance(contract, dict) else "") == _REPAIR_CONTRACT_SCHEMA:
            return _repair_identity(contract)
    return ()


def _idempotency_identity(contract: dict[str, Any]) -> tuple[object, ...]:
    return (
        _IDEMPOTENCY_CONTRACT_SCHEMA,
        text_value(contract.get("kind")),
        text_value(contract.get("idempotency_key") or contract.get("key")),
        _string_tuple(contract.get("scope_refs"), normalize_path=True),
    )


def _repair_identity(contract: dict[str, Any]) -> tuple[object, ...]:
    targets = _string_tuple(contract.get("target_artifact_refs"), normalize_path=True)
    required = _string_tuple(contract.get("required_read_paths"), normalize_path=True)
    return (
        _REPAIR_CONTRACT_SCHEMA,
        text_value(contract.get("kind")),
        _string_tuple(contract.get("failed_run_ids")),
        targets,
        required if not targets else (),
    )


def _iter_packs(value: object):
    if isinstance(value, dict):
        yield value
        return
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, dict):
            yield item


def _string_tuple(value: object, *, normalize_path: bool = False) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value]
    normalized = []
    for item in values:
        text = _normalized_path(item) if normalize_path else text_value(item)
        if text:
            normalized.append(text)
    return tuple(sorted(dict.fromkeys(normalized)))


def _normalized_path(value: object) -> str:
    text = text_value(value)
    if text == "/":
        return text
    return text.rstrip("/")


__all__ = [
    "idempotency_contract_identity_from_context_packs",
    "repair_contract_identity_from_context_packs",
]
