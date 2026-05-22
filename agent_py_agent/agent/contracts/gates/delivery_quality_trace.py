# LLM: Delivery quality trace events keep closeout gates replayable.
# 模块用途: 将 delivery_quality 门结果写入 append-only JSONL，供真实 run 复盘和 replay 使用。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import GateDecision


# LLM: DeliveryQualityTraceScope carries run identity for one trace event.
# 类用途: 避免 trace helper 参数膨胀，并保持 run/task/contract_hash 结构字段集中。
@dataclass(frozen=True)
class DeliveryQualityTraceScope:
    run_id: str
    task_id: str
    contract_hash: str


# LLM: append_delivery_quality_gate_trace stores one compact replay/audit event.
# 函数用途: 把质量门决策追加到 jsonl，后续真实任务复盘可直接读取 finding_codes。
def append_delivery_quality_gate_trace(
    trace_path: Path,
    decision: GateDecision,
    scope: DeliveryQualityTraceScope,
) -> None:
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": "delivery_quality_gate",
        "run_id": scope.run_id,
        "task_id": scope.task_id,
        "contract_hash": scope.contract_hash,
        "gate": decision.gate,
        "status": decision.status,
        "allowed": decision.allowed,
        "finding_codes": list(decision.finding_codes),
    }
    with trace_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n")


__all__ = ["DeliveryQualityTraceScope", "append_delivery_quality_gate_trace"]
