
from __future__ import annotations

from typing import Any

from ...common.value_parsing import text_value

_IDEMPOTENCY_CONTRACT_SCHEMA = "subagent_idempotency_contract.v1"


def idempotency_contract_identity_from_context_packs(value: object) -> tuple[object, ...]:
    for pack in _iter_packs(value):
        contract = pack.get("contract")
        if text_value(contract.get("schema") if isinstance(contract, dict) else "") == _IDEMPOTENCY_CONTRACT_SCHEMA:
            return _contract_identity(contract)
    return ()


def _contract_identity(contract: dict[str, Any]) -> tuple[object, ...]:
    return (
        _IDEMPOTENCY_CONTRACT_SCHEMA,
        text_value(contract.get("kind")),
        text_value(contract.get("idempotency_key") or contract.get("key")),
        _string_tuple(contract.get("scope_refs"), normalize_path=True),
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


__all__ = ["idempotency_contract_identity_from_context_packs"]
