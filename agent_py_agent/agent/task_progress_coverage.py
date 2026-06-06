
from __future__ import annotations

import re
from typing import Any

from .common.value_parsing import dedupe_strings, string_list

_DONE_STATUSES = {"done", "skipped"}


def normalize_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    raw_coverage = payload.get("coverage")
    coverage = dict(raw_coverage) if isinstance(raw_coverage, dict) else {}
    dimensions = string_list(coverage.get("dimensions") or payload.get("coverage_dimensions"))
    targets = _list(coverage.get("targets")) or _list(payload.get("coverage_targets"))
    normalized_targets = [_normalize_coverage_target(item) for item in targets]
    normalized = {
        "goal": str(
            coverage.get("goal")
            or payload.get("coverage_goal")
            or (raw_coverage if isinstance(raw_coverage, str) else "")
        ).strip(),
        "dimensions": dimensions,
        "targets": normalized_targets,
    }
    requirement = str(coverage.get("coverage_requirement") or payload.get("coverage_requirement") or "").strip()
    if requirement:
        normalized["coverage_requirement"] = requirement
    enforcement = str(coverage.get("enforcement") or payload.get("coverage_enforcement") or "").strip()
    if enforcement:
        normalized["enforcement"] = enforcement
    normalized["counts"] = _coverage_counts(normalized_targets)
    return normalized


def coverage_from_update(update: dict[str, Any]) -> dict[str, Any]:
    coverage = normalize_coverage(update)
    item_targets = [_item_as_coverage_target(item) for item in _list(update.get("items"))]
    item_targets = [target for target in item_targets if target]
    if item_targets:
        coverage["targets"] = merge_coverage_targets(coverage["targets"], item_targets)
        coverage["counts"] = _coverage_counts(coverage["targets"])
    return coverage


def merge_coverage(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    existing = normalize_coverage({"coverage": existing})
    incoming = normalize_coverage({"coverage": incoming})
    targets = merge_coverage_targets(existing["targets"], incoming["targets"])
    merged = {
        "goal": incoming["goal"] or existing["goal"],
        "dimensions": dedupe_strings([*existing["dimensions"], *incoming["dimensions"]]),
        "targets": targets,
    }
    if incoming.get("coverage_requirement") or existing.get("coverage_requirement"):
        merged["coverage_requirement"] = incoming.get("coverage_requirement") or existing.get("coverage_requirement")
    if incoming.get("enforcement") or existing.get("enforcement"):
        merged["enforcement"] = incoming.get("enforcement") or existing.get("enforcement")
    merged["counts"] = _coverage_counts(targets)
    return merged


def coverage_summary(coverage: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_coverage({"coverage": coverage})
    active = [target for target in normalized["targets"] if not _coverage_target_done(target)][:12]
    summary = {
        "goal": normalized["goal"],
        "dimensions": normalized["dimensions"],
        "counts": normalized["counts"],
        "active_targets": [_coverage_target_summary(target) for target in active],
    }
    if normalized.get("coverage_requirement"):
        summary["coverage_requirement"] = normalized["coverage_requirement"]
    if normalized.get("enforcement"):
        summary["enforcement"] = normalized["enforcement"]
    return summary


def merge_coverage_targets(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(item.get("id") or ""): dict(item) for item in existing if str(item.get("id") or "")}
    order = [str(item.get("id") or "") for item in existing if str(item.get("id") or "")]
    for item in incoming:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        if item_id not in by_id:
            order.append(item_id)
            by_id[item_id] = item
            continue
        previous = by_id[item_id]
        by_id[item_id] = {
            **previous,
            **{key: value for key, value in item.items() if value not in ("", [], {}, None)},
            "checks": {**dict(previous.get("checks") or {}), **dict(item.get("checks") or {})},
            "evidence": dedupe_strings([*string_list(previous.get("evidence")), *string_list(item.get("evidence"))]),
        }
        if _coverage_target_done(previous) and not _coverage_target_done(item):
            by_id[item_id]["status"] = previous.get("status") or "done"
            by_id[item_id]["checks"] = _preserve_done_checks(previous, by_id[item_id])
    return [by_id[item_id] for item_id in order if item_id in by_id]


def _preserve_done_checks(previous: dict[str, Any], merged: dict[str, Any]) -> dict[str, str]:
    checks = dict(merged.get("checks") or {})
    for key, status in dict(previous.get("checks") or {}).items():
        if _is_done_status(status):
            checks[key] = str(status)
    return checks


def _item_as_coverage_target(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    fields = _fields_to_checks(_first_present(value, ("fields", "expected_fields", "fields_needed")))
    if not fields:
        return {}
    item = dict(value)
    item["checks"] = fields
    return _normalize_coverage_target(item)


def _normalize_coverage_target(value: object) -> dict[str, Any]:
    item = dict(value) if isinstance(value, dict) else _coverage_target_from_text(value)
    target_id = str(item.get("id") or item.get("name") or item.get("title") or "").strip()
    title = str(item.get("title") or item.get("name") or target_id).strip()
    result = {
        "id": target_id or _safe_id(title) or "target",
        "title": title,
        "status": str(item.get("status") or "pending").strip() or "pending",
        "checks": _normalize_target_checks(item),
        "evidence": string_list(item.get("evidence")),
        "notes": str(item.get("notes") or "").strip(),
        "next": str(item.get("next") or "").strip(),
    }
    for key in ("owner", "priority", "updated_at", "coverage_kind", "source_ref"):
        if key in item:
            result[key] = item[key]
    return result


def _coverage_target_from_text(value: object) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {"title": ""}
    delimiter = ":" if ":" in text else "：" if "：" in text else ""
    if delimiter:
        target_id, fields_text = text.split(delimiter, 1)
        return {
            "id": target_id.strip(),
            "title": target_id.strip(),
            "checks": dict.fromkeys(_split_field_text(fields_text), "pending"),
        }
    return {"title": text}


def _normalize_target_checks(item: dict[str, Any]) -> dict[str, str]:
    checks = _normalize_checks(item.get("checks"))
    if checks:
        return checks
    return _fields_to_checks(
        _first_present(item, ("expected_fields", "fields", "fields_needed", "missing_fields"))
    )


def _normalize_checks(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        return _checks_from_mapping(value)
    if isinstance(value, list | tuple):
        return _checks_from_sequence(value)
    return {}


def _checks_from_mapping(value: dict) -> dict[str, str]:
    return {
        str(key).strip(): str(status or "pending").strip() or "pending"
        for key, status in value.items()
        if str(key).strip()
    }


def _checks_from_sequence(value: list | tuple) -> dict[str, str]:
    checks: dict[str, str] = {}
    for item in value:
        key = _check_key(item) if isinstance(item, dict) else str(item).strip()
        if key:
            status = str(item.get("status") or "pending").strip() if isinstance(item, dict) else "pending"
            checks[key] = status or "pending"
    return checks


def _check_key(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("title") or item.get("name") or "").strip()


def _fields_to_checks(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        return _normalize_checks(value)
    return dict.fromkeys(string_list(value), "pending")


def _coverage_target_summary(target: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "id": str(target.get("id") or ""),
        "title": str(target.get("title") or ""),
        "status": str(target.get("status") or ""),
        "checks": dict(target.get("checks") or {}),
        "next": str(target.get("next") or ""),
    }
    for key in ("coverage_kind", "source_ref"):
        if target.get(key):
            summary[key] = str(target.get(key) or "")
    return summary


def _coverage_counts(targets: list[dict[str, Any]]) -> dict[str, int]:
    checks = [status for target in targets for status in dict(target.get("checks") or {}).values()]
    return {
        "targets_total": len(targets),
        "targets_done": sum(1 for target in targets if _coverage_target_done(target)),
        "targets_incomplete": sum(1 for target in targets if not _coverage_target_done(target)),
        "checks_total": len(checks),
        "checks_done": sum(1 for status in checks if _is_done_status(status)),
        "checks_incomplete": sum(1 for status in checks if not _is_done_status(status)),
    }


def _coverage_target_done(target: dict[str, Any]) -> bool:
    checks = dict(target.get("checks") or {})
    if checks:
        return all(_is_done_status(status) for status in checks.values())
    return _is_done_status(target.get("status"))


def _is_done_status(value: object) -> bool:
    return str(value or "").strip().lower() in _DONE_STATUSES


def _split_field_text(value: str) -> list[str]:
    text = str(value or "").strip()
    for separator in ("，", "、", ";", "；", "|"):
        text = text.replace(separator, ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> object:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _list(value: object) -> list:
    return list(value) if isinstance(value, list | tuple) else []


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-") or "main"


__all__ = ["coverage_from_update", "coverage_summary", "merge_coverage", "normalize_coverage"]
