
from __future__ import annotations

from typing import Any


def collect_target_coverage_records(payloads: list[object]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for payload in payloads:
        _collect_records(payload, records)
    return _unique_records(records)


def target_coverage_status(
    contract: dict[str, Any],
    *,
    coverage_records: list[dict[str, object]],
) -> dict[str, object]:
    targets = _target_items(contract)
    covered_ids = {
        str(record.get("target_id") or "").strip()
        for record in coverage_records
        if _record_counts_as_covered(record)
    }
    missing = [item for item in targets if str(item.get("target_id") or "").strip() not in covered_ids]
    return {
        "scope_label": str(contract.get("scope_label") or ""),
        "enforcement": str(contract.get("enforcement") or "advisory"),
        "expected_count": len(targets),
        "covered_count": len(targets) - len(missing),
        "missing_count": len(missing),
        "missing_items": missing[:50],
        "coverage_records": coverage_records[:100],
        "should_block": False,
        "recommended_next_action": "continue_or_summarize_with_missing_items_visible",
    }


def _collect_records(value: object, records: list[dict[str, object]]) -> None:
    if isinstance(value, dict):
        _collect_from_mapping(value, records)
    for child in _children(value):
        _collect_records(child, records)


def _children(value: object) -> list[object]:
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list | tuple):
        return list(value)
    return []


def _collect_from_mapping(value: dict[str, object], records: list[dict[str, object]]) -> None:
    records.extend(_direct_coverage_records(value.get("coverage_records")))
    records.extend(_registry_coverage_records(value.get("artifact_registry_refs")))


def _direct_coverage_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [
        dict(row)
        for row in value
        if isinstance(row, dict) and str(row.get("target_id") or "").strip()
    ]


def _registry_coverage_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, object]] = []
    for row in value:
        if isinstance(row, dict):
            records.extend(_records_from_registry_row(row))
    return records


def _records_from_registry_row(row: dict[str, object]) -> list[dict[str, object]]:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return []
    coverage = metadata.get("coverage_items")
    if not isinstance(coverage, list | tuple | set):
        return []
    artifact_ref = str(row.get("path") or row.get("artifact_id") or "")
    return [
        {
            "target_id": str(item).strip(),
            "status": "covered",
            "artifact_ref": artifact_ref,
        }
        for item in coverage
        if str(item).strip()
    ]


def _target_items(contract: dict[str, Any]) -> list[dict[str, str]]:
    raw = contract.get("target_items")
    if not isinstance(raw, list):
        return []
    return [row for item in raw if (row := _target_item_row(item))]


def _target_item_row(item: object) -> dict[str, str]:
    if isinstance(item, dict):
        target_id = str(item.get("target_id") or item.get("id") or item.get("label") or "").strip()
        return {"target_id": target_id, "label": str(item.get("label") or target_id)} if target_id else {}
    target_id = str(item or "").strip()
    return {"target_id": target_id, "label": target_id} if target_id else {}


def _record_counts_as_covered(record: dict[str, object]) -> bool:
    status = str(record.get("status") or "covered").strip().lower()
    return bool(str(record.get("target_id") or "").strip()) and status in {"covered", "done", "ok", "ready", "hit"}


def _unique_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    unique: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        target_id = str(record.get("target_id") or "").strip()
        artifact_ref = str(record.get("artifact_ref") or record.get("source_ref") or "").strip()
        key = (target_id, artifact_ref)
        if not target_id or key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique
