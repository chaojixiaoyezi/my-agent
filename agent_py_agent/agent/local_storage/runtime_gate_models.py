# LLM: Runtime gate ledger models are persisted replay facts, not prompt prose.
# 模块用途: 定义工具入口 gate 审计账本的数据结构，供 resume/replay/idempotency 读取。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# LLM: RuntimeGateLedgerRecord is one durable tool-entry gate record.
# 类用途: 保存一次工具操作的 gate、参数 hash、审批和幂等事实。
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

