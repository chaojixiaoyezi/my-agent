
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RuntimeGateLedgerRecord:
    run_id: str
    task_id: str
    operation_id: str
    tool: str
    parameters: dict[str, Any] = field(default_factory=dict)
    runtime_gate: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    args_hash: str = ""
    approval_id: str = ""
    result_ref: str = ""
    status: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


__all__ = ["RuntimeGateLedgerRecord"]

