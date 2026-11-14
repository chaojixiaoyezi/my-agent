
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


@dataclass(frozen=True)
class BusinessEffectIdentity:
    """宿主派生的同任务业务项身份；不承载授权，实参另由原账严格比较。"""

    owner_id: str
    task_id: str
    scope_key: str
    item_key: str

    def __post_init__(self):
        for value in (self.owner_id, self.task_id, self.scope_key, self.item_key):
            if not isinstance(value, str) or not value.strip() or len(value) > 4096:
                raise ValueError('业务幂等身份要求非空且有界的owner/task/scope/item')

    @property
    def key(self) -> str:
        return idempotency_key('business-item', self.__dict__)

    @property
    def operation_id(self) -> str:
        return f'op:business-item:{self.key.rsplit(":", 1)[-1]}'


def idempotency_key(operation: str, payload: dict[str, Any]) -> str:
    digest = _stable_digest({"operation": str(operation or ""), "payload": _normalize(payload)})
    return f"idem:{operation}:{digest}"


def operation_id(operation: str, payload: dict[str, Any]) -> str:
    digest = _stable_digest({"operation": str(operation or ""), "payload": _normalize(payload)})
    return f"op:{operation}:{digest[:16]}"


def operation_idempotency_key(run_id: str, operation_id: str) -> str:
    """Return the stable key for one explicitly identified execution attempt.

    This deliberately does not hash tool arguments.  Two intentional calls
    with identical arguments are different operations; only a replay carrying
    the same run and operation identity is deduplicated.
    """

    digest = _stable_digest(
        {
            "scope": str(run_id or "unscoped"),
            "operation_id": str(operation_id or ""),
        }
    )
    return f"idem:operation:{digest}"


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


__all__ = [
    "IdempotencyDecision",
    "decide_idempotency",
    "idempotency_key",
    "operation_id",
    "operation_idempotency_key",
]
