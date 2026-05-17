# LLM: Compact apply lineage records repeated compact cycles without rewriting old apply artifacts.
# 模块用途: 从 append-only ledger 推导本轮 compact 是第几次、上一包是谁，支撑长任务多次压缩恢复。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# LLM: CompactApplyLineageRequest is the bundle for deriving lineage from the apply ledger.
# 类用途: 汇总当前 apply 引用和 ledger 路径，避免 lineage helper 读取或修改无关 compact 文件。
@dataclass(frozen=True)
class CompactApplyLineageRequest:
    ledger_path: Path
    plan_id: str
    apply_id: str
    metadata_ref: str
    apply_bundle_ref: str


# LLM: build_compact_apply_lineage gives every repeated compact a machine-readable previous pointer.
# 函数用途: 读取同 plan_id 的上一条 ledger，生成 cycle_index 和 previous refs；不写文件、不读取正文。
def build_compact_apply_lineage(request: CompactApplyLineageRequest) -> dict[str, Any]:
    previous = _latest_record_for_plan(request.ledger_path, request.plan_id)
    previous_lineage = previous.get("lineage", {}) if isinstance(previous.get("lineage"), dict) else {}
    previous_cycle = _positive_int(previous_lineage.get("cycle_index"))
    if previous and previous_cycle == 0:
        previous_cycle = _count_records_for_plan(request.ledger_path, request.plan_id)
    refs = previous.get("refs", {}) if isinstance(previous.get("refs"), dict) else {}
    previous_apply_id = str(previous.get("apply_id") or "")
    return {
        "schema_version": 1,
        "status": "continued" if previous_apply_id else "root",
        "plan_id": request.plan_id,
        "current_apply_id": request.apply_id,
        "current_metadata_ref": request.metadata_ref,
        "current_apply_bundle_ref": request.apply_bundle_ref,
        "cycle_index": previous_cycle + 1 if previous_apply_id else 1,
        "previous_apply_id": previous_apply_id,
        "previous_metadata_ref": str(refs.get("metadata") or ""),
        "previous_apply_bundle_ref": str(refs.get("apply_bundle") or ""),
        "content_preserved": True,
    }


# LLM: _latest_record_for_plan scans the compact ledger as an append-only event stream.
# 函数用途: 找到同一 plan_id 最近一次 apply 记录；坏行直接跳过，让恢复链路尽量可用。
def _latest_record_for_plan(path: Path, plan_id: str) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for record in _iter_jsonl_records(path):
        if str(record.get("plan_id") or "") == plan_id:
            latest = record
    return latest


# LLM: _count_records_for_plan is a compatibility fallback for ledgers written before lineage existed.
# 函数用途: 统计同 plan_id 的历史记录数量，用于旧 ledger 没有 cycle_index 时继续编号。
def _count_records_for_plan(path: Path, plan_id: str) -> int:
    return sum(1 for record in _iter_jsonl_records(path) if str(record.get("plan_id") or "") == plan_id)


# LLM: _iter_jsonl_records keeps corrupt ledger rows from breaking future compact applies.
# 函数用途: 宽容读取 JSONL 对象行；文件缺失、坏 JSON、非对象行都不会让 apply 失败。
def _iter_jsonl_records(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


# LLM: _positive_int normalizes old or malformed lineage counters.
# 函数用途: 将 cycle_index 转成非负整数，异常输入视为 0 以便重新推导。
def _positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


__all__ = ["CompactApplyLineageRequest", "build_compact_apply_lineage"]
