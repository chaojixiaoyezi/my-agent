
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .common.value_parsing import dedupe_strings, string_list
from .runtime_errors import DataCorruptionError, runtime_error_report
from .task_progress_coverage import (
    coverage_from_update,
    coverage_summary,
    merge_coverage,
    normalize_coverage,
)
from .task_progress_hints import quality_hints

_SCHEMA_VERSION = "task_progress.v1"
_KNOWN_STATUSES = ("pending", "in_progress", "done", "skipped", "blocked")
_DONE_LIKE_STATUSES = {"done", "skipped"}
_STATUS_ALIASES = {
    "done": {
        "done",
        "complete",
        "completed",
        "ok",
        "passed",
        "read",
        "read_done",
        "finish",
        "finished",
        "完成",
        "已完成",
        "读完",
        "已读",
        "已读取",
    },
    "pending": {
        "pending",
        "todo",
        "to_do",
        "to-read",
        "to_read",
        "unread",
        "not_started",
        "待处理",
        "待办",
        "未读",
        "待读",
        "待读取",
        "未开始",
    },
    "in_progress": {
        "in_progress",
        "in-progress",
        "doing",
        "reading",
        "processing",
        "进行中",
        "读取中",
        "处理中",
        "正在读",
        "正在读取",
    },
    "skipped": {
        "skipped",
        "skip",
        "ignored",
        "忽略",
        "跳过",
        "已跳过",
    },
    "blocked": {
        "blocked",
        "blocker",
        "failed",
        "error",
        "卡住",
        "阻塞",
        "失败",
        "报错",
    },
}
_FACT_FIELDS = ("id", "title", "status", "notes", "result", "outcome", "conclusion", "decision", "summary")
_EXPLICIT_OVERWRITE_KEYS = ("correction", "overwrite", "replace")


def progress_path(root: str | Path, run_id: str) -> Path:
    return Path(root) / "memory_archive" / "task_progress" / _safe_id(run_id) / "progress.json"


def read_task_progress(root: str | Path, run_id: str) -> dict[str, Any]:
    progress, _load_error = read_task_progress_report(root, run_id)
    return progress


def read_task_progress_report(root: str | Path, run_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload, load_error = _read_json_file_report(progress_path(root, run_id))
    if not payload:
        progress = _empty_progress(run_id)
        if load_error:
            progress["load_error"] = load_error
        return progress, load_error
    progress = normalize_task_progress(payload, run_id=run_id)
    if load_error:
        progress["load_error"] = load_error
    return progress, load_error


def _normalize_existing_task_progress(root: str | Path, run_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload, load_error = _read_json_file_report(progress_path(root, run_id))
    if not payload:
        progress = _empty_progress(run_id)
        if load_error:
            progress["load_error"] = load_error
        return progress, load_error
    return normalize_task_progress(payload, run_id=run_id), load_error


def write_task_progress(root: str | Path, run_id: str, update: dict[str, Any]) -> dict[str, Any]:
    existing, load_error = _normalize_existing_task_progress(root, run_id)
    merged = merge_task_progress(existing, update, run_id=run_id)
    if load_error:
        merged["load_errors"] = [load_error]
    _write_json_file_atomic(progress_path(root, run_id), merged)
    return merged


def task_progress_summary(progress: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_task_progress(progress, run_id=str(progress.get("run_id") or ""))
    active = [
        _summary_item(item)
        for item in normalized["items"]
        if str(item.get("status") or "") not in {"done", "skipped"}
    ][:8]
    recent_done = [
        _summary_item(item, include_facts=True)
        for item in normalized["items"]
        if _done_like_status(item.get("status"))
    ][-24:]
    summary = {
        "schema_version": _SCHEMA_VERSION,
        "run_id": normalized["run_id"],
        "summary": normalized["summary"],
        "next_action": normalized["next_action"],
        "counts": normalized["counts"],
        "active_items": active,
        "recent_done_items": recent_done,
        "updated_at": normalized["updated_at"],
        "ref": str(normalized.get("ref") or ""),
    }
    if normalized.get("quality_hints"):
        summary["quality_hints"] = normalized["quality_hints"]
    if normalized.get("coverage"):
        summary["coverage"] = coverage_summary(normalized["coverage"])
    if normalized.get("load_error"):
        summary["load_error"] = normalized["load_error"]
    return summary


def normalize_task_progress(payload: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    items = [_normalize_item(item) for item in _list(payload.get("items"))]
    coverage = normalize_coverage(payload)
    normalized = {
        "schema_version": _SCHEMA_VERSION,
        "run_id": str(payload.get("run_id") or run_id or "main"),
        "summary": str(payload.get("summary") or "").strip(),
        "next_action": str(payload.get("next_action") or "").strip(),
        "items": items,
        "counts": _counts(items),
        "updated_at": float(payload.get("updated_at") or 0.0),
    }
    hints = quality_hints(items, coverage=coverage)
    if hints["messages"]:
        normalized["quality_hints"] = hints
    if coverage["targets"] or coverage["goal"] or coverage["dimensions"]:
        normalized["coverage"] = coverage
    load_error = payload.get("load_error") or _first_load_error(payload.get("load_errors"))
    if isinstance(load_error, dict):
        normalized["load_error"] = load_error
    ref = str(payload.get("ref") or "").strip()
    if ref:
        normalized["ref"] = ref
    return normalized


def merge_task_progress(existing: dict[str, Any], update: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    base = normalize_task_progress(existing, run_id=run_id)
    merged_items = _merge_items(base["items"], [_normalize_item(item) for item in _list(update.get("items"))])
    coverage = merge_coverage(base.get("coverage", {}), coverage_from_update(update))
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "run_id": base["run_id"] or run_id or "main",
        "summary": str(update.get("summary") or base.get("summary") or "").strip(),
        "next_action": str(update.get("next_action") or base.get("next_action") or "").strip(),
        "items": merged_items,
        "updated_at": time.time(),
    }
    payload["counts"] = _counts(merged_items)
    hints = quality_hints(
        merged_items,
        incoming=[_normalize_item(item) for item in _list(update.get("items"))],
        coverage=coverage,
    )
    _apply_soft_next_action_repair(payload, hints)
    if hints["messages"]:
        payload["quality_hints"] = hints
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
    status = _normalize_status_value(item.get("status") or "pending")
    result = {
        "id": item_id or _safe_id(title) or "item",
        "title": title,
        "status": status,
        "notes": str(item.get("notes") or item.get("note") or "").strip(),
        "next": str(item.get("next") or "").strip(),
        "evidence": string_list(item.get("evidence")),
    }
    for key in ("result", "outcome", "conclusion", "decision", "summary"):
        text = str(item.get(key) or "").strip()
        if text:
            result[key] = text
    for key in ("updated_at", "owner", "priority"):
        if key in item:
            result[key] = item[key]
    for key in _EXPLICIT_OVERWRITE_KEYS:
        if _truthy(item.get(key)):
            result[key] = True
    return result


def _merge_items(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in existing:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        key = _merge_key(item)
        if key not in by_id:
            order.append(key)
            by_id[key] = dict(item)
    for item in incoming:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        key = _merge_key(item)
        if key not in by_id:
            order.append(key)
            by_id[key] = item
            continue
        previous = by_id[key]
        if _should_preserve_done_facts(previous, item):
            by_id[key] = _merge_done_item_without_overwriting_facts(previous, item)
            continue
        by_id[key] = _merge_item_overlay(previous, item)
    _remove_superseded_range_items(by_id, order)
    return [by_id[item_id] for item_id in order if item_id in by_id]


def _merge_item_overlay(previous: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = {**previous, **{key: value for key, value in incoming.items() if value not in ("", [], None)}}
    if incoming.get("evidence") or previous.get("evidence"):
        merged["evidence"] = dedupe_strings([*string_list(previous.get("evidence")), *string_list(incoming.get("evidence"))])
    return merged


def _should_preserve_done_facts(previous: dict[str, Any], incoming: dict[str, Any]) -> bool:
    return (
        _done_like_status(previous.get("status"))
        and not any(_truthy(incoming.get(key)) for key in _EXPLICIT_OVERWRITE_KEYS)
    )


def _merge_done_item_without_overwriting_facts(previous: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = _merge_item_overlay(previous, incoming)
    for key in _FACT_FIELDS:
        if previous.get(key) not in ("", [], None):
            merged[key] = previous[key]
    merged["evidence"] = dedupe_strings([*string_list(previous.get("evidence")), *string_list(incoming.get("evidence"))])
    return merged


def _apply_soft_next_action_repair(payload: dict[str, Any], hints: dict[str, Any]) -> None:
    if not _closeoutish_next_action(payload.get("next_action")):
        return
    suggestions = [str(item).strip() for item in hints.get("next_suggestions", []) if str(item).strip()]
    if not suggestions:
        return
    original = str(payload.get("next_action") or "").strip()
    payload["next_action"] = suggestions[0]
    payload["soft_next_action_repair"] = {
        "severity": "soft",
        "blocking": False,
        "original_next_action": original,
        "message": "进度账本还存在证据或覆盖提醒，下一步先继续补证据/覆盖项，不要直接提交验收。",
    }


def _closeoutish_next_action(value: object) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "submit",
            "acceptance",
            "closeout",
            "final",
            "提交验收",
            "验收",
            "收口",
            "交付",
            "完成任务",
        )
    )


def _summary_item(item: dict[str, Any], *, include_facts: bool = False) -> dict[str, Any]:
    summary = {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "status": str(item.get("status") or ""),
        "next": str(item.get("next") or ""),
    }
    if include_facts:
        for key in ("notes", "result", "outcome", "conclusion", "decision", "summary"):
            text = str(item.get(key) or "").strip()
            if text:
                summary[key] = text
        evidence = string_list(item.get("evidence"))[:8]
        if evidence:
            summary["evidence"] = evidence
    return summary


def _done_like_status(value: object) -> bool:
    return _normalize_status_value(value) in _DONE_LIKE_STATUSES


def _normalize_status_value(value: object) -> str:
    text = str(value or "").strip()
    normalized = text.lower().replace(" ", "_")
    for status, aliases in _STATUS_ALIASES.items():
        if normalized in aliases or text in aliases:
            return status
    return text or "pending"


def _merge_key(item: dict[str, Any]) -> str:
    item_id = str(item.get("id") or "").strip()
    title = str(item.get("title") or "").strip()
    for value in (item_id, title):
        if key := _fragment_alias_key(value):
            return key
    return item_id


def _fragment_alias_key(value: str) -> str:
    text = str(value or "").strip().lower()
    match = re.fullmatch(r"(?:fragment|frag)?[-_ ]?(\d{1,6})", text)
    if not match:
        return ""
    return f"fragment-{int(match.group(1)):03d}"


def _remove_superseded_range_items(by_id: dict[str, dict[str, Any]], order: list[str]) -> None:
    done_parts = {
        part
        for item in by_id.values()
        if _done_like_status(item.get("status"))
        for part in _numeric_item_parts(item)
    }
    continuation_starts = [
        (key, *continuation)
        for key, item in by_id.items()
        if not _done_like_status(item.get("status"))
        if (continuation := _numeric_continuation_key(item))
    ]
    _remove_superseded_continuation_items(by_id, order, continuation_starts, done_parts)
    if not done_parts and not continuation_starts:
        return
    for key, item in list(by_id.items()):
        if _done_like_status(item.get("status")):
            continue
        range_key = _numeric_range_key(item)
        if not range_key:
            continue
        prefix, start, end = range_key
        if start > end or end - start > 2000:
            continue
        if all((prefix, number) in done_parts for number in range(start, end + 1)):
            by_id.pop(key, None)
            if key in order:
                order.remove(key)
            continue
        if _range_has_newer_continuation(prefix, start, end, continuation_starts, done_parts):
            by_id.pop(key, None)
            if key in order:
                order.remove(key)


def _remove_superseded_continuation_items(
    by_id: dict[str, dict[str, Any]],
    order: list[str],
    continuation_starts: list[tuple[str, str, int]],
    done_parts: set[tuple[str, int]],
) -> None:
    by_prefix: dict[str, list[tuple[str, int]]] = {}
    for key, prefix, start in continuation_starts:
        by_prefix.setdefault(prefix, []).append((key, start))
    for prefix, values in by_prefix.items():
        if len(values) < 2:
            continue
        latest_start = max(start for _, start in values)
        for key, start in values:
            if start >= latest_start:
                continue
            if done_parts and not all((prefix, number) in done_parts for number in range(start, latest_start)):
                continue
            by_id.pop(key, None)
            if key in order:
                order.remove(key)


def _range_has_newer_continuation(
    prefix: str,
    start: int,
    end: int,
    continuation_starts: list[tuple[str, str, int]],
    done_parts: set[tuple[str, int]],
) -> bool:
    for _key, continuation_prefix, continuation_start in continuation_starts:
        if continuation_prefix != prefix:
            continue
        if continuation_start <= start or continuation_start > end + 1:
            continue
        if done_parts and not all((prefix, number) in done_parts for number in range(start, continuation_start)):
            continue
        return True
    return False


def _numeric_item_parts(item: dict[str, Any]) -> set[tuple[str, int]]:
    parts: set[tuple[str, int]] = set()
    for key in ("id", "title"):
        text = str(item.get(key) or "")
        if part := _numeric_item_key(text):
            parts.add(part)
    return parts


def _numeric_item_key(value: str) -> tuple[str, int] | None:
    text = str(value or "").strip().lower()
    match = re.match(r"^([a-z\u4e00-\u9fff_-]*?)[-_ ]?(\d{1,6})(?:\D.*)?$", text)
    if not match:
        return None
    return (_normalize_numeric_prefix(match.group(1)), int(match.group(2)))


def _numeric_range_key(item: dict[str, Any]) -> tuple[str, int, int] | None:
    for key in ("id", "title"):
        text = str(item.get(key) or "").strip().lower()
        match = re.match(
            r"^([a-z\u4e00-\u9fff_-]*?)[-_ ]?(\d{1,6})\s*(?:-|~|至|到)\s*(?:[a-z\u4e00-\u9fff_-]*?[-_ ]?)?(\d{1,6})(?:\D.*)?$",
            text,
        )
        if match:
            return (_normalize_numeric_prefix(match.group(1)), int(match.group(2)), int(match.group(3)))
    return None


def _numeric_continuation_key(item: dict[str, Any]) -> tuple[str, int] | None:
    for key in ("id", "title"):
        text = str(item.get(key) or "").strip().lower()
        match = re.match(
            r"^([a-z\u4e00-\u9fff_-]*?)[-_ ]?(\d{1,6})\s*(?:\+|及之后|以后|之后|起|后续)(?:\D.*)?$",
            text,
        )
        if match:
            return (_normalize_numeric_prefix(match.group(1)), int(match.group(2)))
    return None


def _normalize_numeric_prefix(value: str) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "chapter": "ch",
        "chap": "ch",
        "章节": "ch",
        "fragment": "fragment",
        "frag": "fragment",
        "片段": "fragment",
    }
    return aliases.get(text, text)


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "是", "对"}


def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {"total": len(items)}
    for item in items:
        status = str(item.get("status") or "pending").strip() or "pending"
        key = status if status in _KNOWN_STATUSES else "other"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _list(value: object) -> list:
    return list(value) if isinstance(value, list | tuple) else []


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-") or "main"


def _read_json_file_report(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, runtime_error_report(exc, context="task_progress.read")
    if not isinstance(payload, dict):
        exc = DataCorruptionError(f"task_progress root must be a JSON object: {path}")
        return {}, runtime_error_report(exc, context="task_progress.read")
    return payload, None


def _first_load_error(value: object) -> dict[str, Any] | None:
    items = value if isinstance(value, list) else []
    return next((item for item in items if isinstance(item, dict)), None)


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
    "read_task_progress_report",
    "task_progress_summary",
    "write_task_progress",
]
