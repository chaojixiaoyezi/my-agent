# LLM: Task progress is a soft ledger for long work, not an acceptance gate.
# 模块用途: 为主代理/子代理/孙代理保存通用进度清单；tree 和 compact 只读取摘要，不据此阻断任务。

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = "task_progress.v1"
_KNOWN_STATUSES = ("pending", "in_progress", "done", "skipped", "blocked")
_DONE_STATUSES = {"done", "complete", "completed", "ok", "passed", "skipped"}


def progress_path(root: str | Path, run_id: str) -> Path:
    return Path(root) / "memory_archive" / "task_progress" / _safe_id(run_id) / "progress.json"


def read_task_progress(root: str | Path, run_id: str) -> dict[str, Any]:
    payload = _read_json_file(progress_path(root, run_id))
    if not payload:
        return _empty_progress(run_id)
    return normalize_task_progress(payload, run_id=run_id)


def write_task_progress(root: str | Path, run_id: str, update: dict[str, Any]) -> dict[str, Any]:
    existing = read_task_progress(root, run_id)
    merged = merge_task_progress(existing, update, run_id=run_id)
    _write_json_file_atomic(progress_path(root, run_id), merged)
    return merged


def task_progress_summary(progress: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_task_progress(progress, run_id=str(progress.get("run_id") or ""))
    active = [
        _summary_item(item)
        for item in normalized["items"]
        if str(item.get("status") or "") not in {"done", "skipped"}
    ][:8]
    summary = {
        "schema_version": _SCHEMA_VERSION,
        "run_id": normalized["run_id"],
        "summary": normalized["summary"],
        "next_action": normalized["next_action"],
        "counts": normalized["counts"],
        "active_items": active,
        "updated_at": normalized["updated_at"],
        "ref": str(normalized.get("ref") or ""),
    }
    if normalized.get("coverage"):
        summary["coverage"] = _coverage_summary(normalized["coverage"])
    return summary


def normalize_task_progress(payload: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    items = [_normalize_item(item) for item in _list(payload.get("items"))]
    coverage = _normalize_coverage(payload)
    normalized = {
        "schema_version": _SCHEMA_VERSION,
        "run_id": str(payload.get("run_id") or run_id or "main"),
        "summary": str(payload.get("summary") or "").strip(),
        "next_action": str(payload.get("next_action") or "").strip(),
        "items": items,
        "counts": _counts(items),
        "updated_at": float(payload.get("updated_at") or 0.0),
    }
    if coverage["targets"] or coverage["goal"] or coverage["dimensions"]:
        normalized["coverage"] = coverage
    ref = str(payload.get("ref") or "").strip()
    if ref:
        normalized["ref"] = ref
    return normalized


def merge_task_progress(existing: dict[str, Any], update: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    base = normalize_task_progress(existing, run_id=run_id)
    merged_items = _merge_items(base["items"], [_normalize_item(item) for item in _list(update.get("items"))])
    coverage = _merge_coverage(base.get("coverage", {}), _coverage_from_update(update))
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "run_id": base["run_id"] or run_id or "main",
        "summary": str(update.get("summary") or base.get("summary") or "").strip(),
        "next_action": str(update.get("next_action") or base.get("next_action") or "").strip(),
        "items": merged_items,
        "updated_at": time.time(),
    }
    payload["counts"] = _counts(merged_items)
    if coverage["targets"] or coverage["goal"] or coverage["dimensions"]:
        payload["coverage"] = coverage
    return payload


def _empty_progress(run_id: str) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "run_id": run_id or "main",
        "summary": "",
        "next_action": "",
        "items": [],
        "counts": {},
        "updated_at": 0.0,
    }


def _normalize_item(value: object) -> dict[str, Any]:
    item = dict(value) if isinstance(value, dict) else {"title": str(value or "").strip()}
    item_id = str(item.get("id") or item.get("title") or "").strip()
    title = str(item.get("title") or item_id).strip()
    status = str(item.get("status") or "pending").strip() or "pending"
    result = {
        "id": item_id or _safe_id(title) or "item",
        "title": title,
        "status": status,
        "notes": str(item.get("notes") or item.get("note") or "").strip(),
        "next": str(item.get("next") or "").strip(),
        "evidence": _string_list(item.get("evidence")),
    }
    for key in ("updated_at", "owner", "priority"):
        if key in item:
            result[key] = item[key]
    return result


def _normalize_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    raw_coverage = payload.get("coverage")
    coverage = dict(raw_coverage) if isinstance(raw_coverage, dict) else {}
    dimensions = _string_list(coverage.get("dimensions") or payload.get("coverage_dimensions"))
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
    normalized["counts"] = _coverage_counts(normalized_targets)
    return normalized


def _coverage_from_update(update: dict[str, Any]) -> dict[str, Any]:
    coverage = _normalize_coverage(update)
    item_targets = [_item_as_coverage_target(item) for item in _list(update.get("items"))]
    item_targets = [target for target in item_targets if target]
    if item_targets:
        coverage["targets"] = _merge_coverage_targets(coverage["targets"], item_targets)
        coverage["counts"] = _coverage_counts(coverage["targets"])
    return coverage


def _item_as_coverage_target(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    fields = _fields_to_checks(_first_present(value, ("fields", "expected_fields", "fields_needed")))
    if not fields:
        fields = _checks_from_note_text(str(value.get("notes") or value.get("note") or ""))
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
        "evidence": _string_list(item.get("evidence")),
        "notes": str(item.get("notes") or "").strip(),
        "next": str(item.get("next") or "").strip(),
    }
    for key in ("owner", "priority", "updated_at"):
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


def _fields_to_checks(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        return _normalize_checks(value)
    return dict.fromkeys(_string_list(value), "pending")


def _split_field_text(value: str) -> list[str]:
    text = str(value or "").strip()
    for separator in ("，", "、", ";", "；", "|"):
        text = text.replace(separator, ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def _checks_from_note_text(value: str) -> dict[str, str]:
    text = str(value or "").strip()
    if not text:
        return {}
    delimiter = "：" if "：" in text else ":" if ":" in text else ""
    if not delimiter:
        return {}
    _, fields_text = text.split(delimiter, 1)
    fields = _split_field_text(fields_text)
    if len(fields) < 2:
        return {}
    return dict.fromkeys(fields, "pending")


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> object:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _normalize_checks(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        return {
            str(key).strip(): str(status or "pending").strip() or "pending"
            for key, status in value.items()
            if str(key).strip()
        }
    if isinstance(value, list | tuple):
        checks: dict[str, str] = {}
        for item in value:
            if isinstance(item, dict):
                key = str(item.get("id") or item.get("title") or item.get("name") or "").strip()
                if key:
                    checks[key] = str(item.get("status") or "pending").strip() or "pending"
            elif text := str(item).strip():
                checks[text] = "pending"
        return checks
    return {}


def _merge_coverage(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    existing = _normalize_coverage({"coverage": existing})
    incoming = _normalize_coverage({"coverage": incoming})
    targets = _merge_coverage_targets(existing["targets"], incoming["targets"])
    merged = {
        "goal": incoming["goal"] or existing["goal"],
        "dimensions": _dedupe([*existing["dimensions"], *incoming["dimensions"]]),
        "targets": targets,
    }
    merged["counts"] = _coverage_counts(targets)
    return merged


def _merge_coverage_targets(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            "evidence": _dedupe([*_string_list(previous.get("evidence")), *_string_list(item.get("evidence"))]),
        }
    return [by_id[item_id] for item_id in order if item_id in by_id]


def _coverage_summary(coverage: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_coverage({"coverage": coverage})
    active = [target for target in normalized["targets"] if not _coverage_target_done(target)][:12]
    return {
        "goal": normalized["goal"],
        "dimensions": normalized["dimensions"],
        "counts": normalized["counts"],
        "active_targets": [_coverage_target_summary(target) for target in active],
    }


def _coverage_target_summary(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(target.get("id") or ""),
        "title": str(target.get("title") or ""),
        "status": str(target.get("status") or ""),
        "checks": dict(target.get("checks") or {}),
        "next": str(target.get("next") or ""),
    }


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


def _merge_items(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        by_id[item_id] = {**by_id[item_id], **{key: value for key, value in item.items() if value not in ("", [], None)}}
    return [by_id[item_id] for item_id in order if item_id in by_id]


def _summary_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "status": str(item.get("status") or ""),
        "next": str(item.get("next") or ""),
    }


def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {"total": len(items)}
    for item in items:
        status = str(item.get("status") or "pending").strip() or "pending"
        key = status if status in _KNOWN_STATUSES else "other"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _list(value: object) -> list:
    return list(value) if isinstance(value, list | tuple) else []


def _string_list(value: object) -> list[str]:
    return [text for item in _list(value) if (text := str(item).strip())]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-") or "main"


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_file_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


__all__ = [
    "merge_task_progress",
    "normalize_task_progress",
    "progress_path",
    "read_task_progress",
    "task_progress_summary",
    "write_task_progress",
]
