
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IdempotencyDecision:
    key: str
    action: str
    existing_ids: list[str] = field(default_factory=list)
    reason: str = ""


def idempotency_key(operation: str, payload: dict[str, Any]) -> str:
    digest = _stable_digest({"operation": str(operation or ""), "payload": _normalize(payload)})
    return f"idem:{operation}:{digest}"


def operation_id(operation: str, payload: dict[str, Any]) -> str:
    digest = _stable_digest({"operation": str(operation or ""), "payload": _normalize(payload)})
    return f"op:{operation}:{digest[:16]}"


def decide_idempotency(operation: str, payload: dict[str, Any], existing_ids: list[str] | None = None) -> IdempotencyDecision:
    key = idempotency_key(operation, payload)
    ids = [str(item) for item in (existing_ids or []) if str(item)]
    if ids:
        return IdempotencyDecision(key=key, action="reuse_existing", existing_ids=ids, reason="same_idempotency_key")
    return IdempotencyDecision(key=key, action="create_new", existing_ids=[], reason="no_existing_match")


def _stable_digest(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _normalize(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _normalize(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = ["IdempotencyDecision", "decide_idempotency", "idempotency_key", "operation_id"]
