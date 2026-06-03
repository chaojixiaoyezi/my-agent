
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CompactApplyLineageRequest:
    ledger_path: Path
    plan_id: str
    apply_id: str
    metadata_ref: str
    apply_bundle_ref: str


def build_compact_apply_lineage(request: CompactApplyLineageRequest) -> dict[str, Any]:
    previous = _latest_record(request.ledger_path)
    if not previous:
        previous = _latest_record_for_plan(request.ledger_path, request.plan_id)
    previous_lineage = previous.get("lineage", {}) if isinstance(previous.get("lineage"), dict) else {}
    previous_cycle = _positive_int(previous_lineage.get("cycle_index"))
    if previous and previous_cycle == 0:
        previous_cycle = _count_records(request.ledger_path)
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


def _latest_record(path: Path) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for record in _iter_jsonl_records(path):
        latest = record
    return latest


def _count_records(path: Path) -> int:
    return len(_iter_jsonl_records(path))


def _latest_record_for_plan(path: Path, plan_id: str) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for record in _iter_jsonl_records(path):
        if str(record.get("plan_id") or "") == plan_id:
            latest = record
    return latest


def _count_records_for_plan(path: Path, plan_id: str) -> int:
    return sum(1 for record in _iter_jsonl_records(path) if str(record.get("plan_id") or "") == plan_id)


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


def _positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


__all__ = ["CompactApplyLineageRequest", "build_compact_apply_lineage"]
