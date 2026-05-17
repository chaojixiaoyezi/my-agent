# LLM: Idempotency contracts make repeated model/tool calls safe without adding workflow guards.
# 模块用途: 生成稳定幂等键和操作编号，让 create/schedule/dispatch/compact/resume 可重复执行不炸。

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


# LLM: IdempotencyDecision reports reuse/create/skip as data for callers to decide next steps.
# 类用途: 表示幂等判断结果；它只描述结果，不创建子代理、不写状态。
@dataclass(frozen=True)
class IdempotencyDecision:
    key: str
    action: str
    existing_ids: list[str] = field(default_factory=list)
    reason: str = ""


# LLM: idempotency_key builds a stable key from operation type and normalized payload.
# 函数用途: 对操作类型和结构化输入做稳定 hash，dict 顺序不影响结果。
def idempotency_key(operation: str, payload: dict[str, Any]) -> str:
    digest = _stable_digest({"operation": str(operation or ""), "payload": _normalize(payload)})
    return f"idem:{operation}:{digest}"


# LLM: operation_id is a human-shorter id for ledgers/envelopes that still derives from the idempotency key.
# 函数用途: 生成可写入 envelope/ledger 的操作编号，方便恢复和重放时去重。
def operation_id(operation: str, payload: dict[str, Any]) -> str:
    digest = _stable_digest({"operation": str(operation or ""), "payload": _normalize(payload)})
    return f"op:{operation}:{digest[:16]}"


# LLM: decide_idempotency converts a known existing-id list into a reusable result payload.
# 函数用途: 当调用方已查到现有 run/artifact/apply 时，统一返回 reuse/create 决策。
def decide_idempotency(operation: str, payload: dict[str, Any], existing_ids: list[str] | None = None) -> IdempotencyDecision:
    key = idempotency_key(operation, payload)
    ids = [str(item) for item in (existing_ids or []) if str(item)]
    if ids:
        return IdempotencyDecision(key=key, action="reuse_existing", existing_ids=ids, reason="same_idempotency_key")
    return IdempotencyDecision(key=key, action="create_new", existing_ids=[], reason="no_existing_match")


# LLM: _stable_digest hashes canonical JSON so Python dict ordering cannot create duplicate operations.
# 函数用途: 生成短 hash；输入必须先 normalize，避免集合/路径等类型破坏 JSON 稳定性。
def _stable_digest(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


# LLM: _normalize keeps only JSON-stable shapes while preserving list order as caller intent.
# 函数用途: 把任意值规整成 JSON 可稳定 hash 的结构，dict 按 key 排序。
def _normalize(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _normalize(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = ["IdempotencyDecision", "decide_idempotency", "idempotency_key", "operation_id"]
