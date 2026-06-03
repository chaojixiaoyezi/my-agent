
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..common.value_parsing import text_or_sequence_strings


def coverage_records_from_payload(payload: dict[str, object]) -> list[dict[str, object]]:
    records = [
        *normalize_coverage_records(payload.get("coverage_records")),
        *normalize_coverage_records(payload.get("coverage")),
    ]
    top_level_coverer = _clean_id(payload.get("covered_by_run_id") or payload.get("covering_run_id"))
    records.extend(_records_from_covered_ids(payload.get("covered_run_ids"), top_level_coverer))
    return _dedupe_records(records)


def normalize_coverage_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        record = _coverage_record_from_item(item)
        if record:
            records.append(record)
    return _dedupe_records(records)


def merge_task_coverage_records(task: object, records: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized = normalize_coverage_records(records)
    if not normalized:
        return task_coverage_records(task)
    attrs = getattr(task, "attributes", None)
    if not isinstance(attrs, dict):
        attrs = {}
        task.attributes = attrs
    merged = _dedupe_records([*normalize_coverage_records(attrs.get("coverage_records")), *normalized])
    attrs["coverage_records"] = merged
    return merged


def task_coverage_records(task: object) -> list[dict[str, object]]:
    attrs = _attributes_dict(task)
    return normalize_coverage_records(attrs.get("coverage_records"))


def all_task_coverage_records(tasks: list[object]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for task in tasks:
        records.extend(task_coverage_records(task))
    return _dedupe_records(records)


def coverage_records_resolve_run(
    run_id: str,
    records: list[dict[str, object]],
    is_verified: Callable[[str], bool],
) -> bool:
    target = _clean_id(run_id)
    if not target:
        return False
    return any(_record_resolves_run(target, record, is_verified) for record in records)


def _coverage_record_from_item(item: dict[str, object]) -> dict[str, object]:
    covered = _clean_id(item.get("covered_run_id") or item.get("source_run_id") or item.get("run_id"))
    coverer = _clean_id(
        item.get("covered_by_run_id")
        or item.get("covering_run_id")
        or item.get("replacement_run_id")
        or item.get("takeover_by")
    )
    if not covered or not coverer:
        return {}
    return {
        "covered_run_id": covered,
        "covered_by_run_id": coverer,
        "reason": str(item.get("reason") or item.get("summary") or "").strip(),
        "artifact_refs": text_or_sequence_strings(item.get("artifact_refs")),
        "evidence_refs": text_or_sequence_strings(item.get("evidence_refs")),
    }


def _records_from_covered_ids(value: object, coverer: str) -> list[dict[str, object]]:
    if not coverer:
        return []
    return [
        {
            "covered_run_id": covered,
            "covered_by_run_id": coverer,
            "reason": "",
            "artifact_refs": [],
            "evidence_refs": [],
        }
        for covered in text_or_sequence_strings(value)
    ]


def _record_resolves_run(target: str, record: dict[str, object], is_verified: Callable[[str], bool]) -> bool:
    if _clean_id(record.get("covered_run_id")) != target:
        return False
    return is_verified(_clean_id(record.get("covered_by_run_id")))


def _attributes_dict(task: object) -> dict[str, object]:
    attrs = task.get("attributes") if isinstance(task, dict) else getattr(task, "attributes", {})
    return attrs if isinstance(attrs, dict) else {}


def _dedupe_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    deduped: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        key = (
            _clean_id(record.get("covered_run_id")),
            _clean_id(record.get("covered_by_run_id")),
            str(record.get("reason") or ""),
        )
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def _clean_id(value: object) -> str:
    return str(value or "").strip()
