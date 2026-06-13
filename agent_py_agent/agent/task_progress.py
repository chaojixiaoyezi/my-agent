
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common.safe_id import safe_id
from .common.value_parsing import dedupe_strings, string_list
from .runtime_errors import DataCorruptionError, runtime_error_report

_SCHEMA_VERSION = "task_progress.v1"
TASK_PROGRESS_KNOWN_STATUSES = ("pending", "in_progress", "done", "skipped", "blocked")
TASK_PROGRESS_UNKNOWN_STATUS = "unknown"
TASK_PROGRESS_COUNT_STATUSES = (*TASK_PROGRESS_KNOWN_STATUSES, TASK_PROGRESS_UNKNOWN_STATUS)
TASK_PROGRESS_CLOSED_STATUSES = frozenset({"done", "skipped"})
TASK_PROGRESS_STATUS_INVALID = "TASK_PROGRESS_STATUS_INVALID"
_FACT_FIELDS = ("id", "title", "status", "notes", "result", "outcome", "conclusion", "decision", "summary")
_RESULT_FIELDS = ("result", "outcome", "conclusion", "decision", "summary")
_EXPLICIT_OVERWRITE_KEYS = ("correction", "overwrite", "replace")


@dataclass(frozen=True)
class _StatusValidationRequest:
    value: object
    field: str
    target_id: str
    check_name: str
    allow_empty: bool


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


def invalid_item_statuses(update: dict[str, Any]) -> list[dict[str, str]]:
    invalid: list[dict[str, str]] = []
    for index, item in enumerate(_list(update.get("items"))):
        if not isinstance(item, dict) or "status" not in item:
            continue
        raw_status = str(item.get("status") or "").strip()
        if not raw_status:
            continue
        if task_progress_status_is_known(raw_status):
            continue
        invalid.append(
            {
                "index": str(index),
                "id": str(item.get("id") or item.get("title") or "").strip(),
                "status": raw_status,
            }
        )
    return invalid


def invalid_coverage_statuses(update: dict[str, Any]) -> list[dict[str, str]]:
    invalid: list[dict[str, str]] = []
    coverage = update.get("coverage")
    targets = coverage.get("targets") if isinstance(coverage, dict) else None
    for target_index, target in enumerate(_list(targets)):
        if not isinstance(target, dict):
            continue
        target_id = str(target.get("id") or target.get("title") or "").strip()
        _append_invalid_status(
            invalid,
            _StatusValidationRequest(
                value=target.get("status"),
                field=f"coverage.targets[{target_index}].status",
                target_id=target_id,
                check_name="",
                allow_empty=True,
            ),
        )
        checks = target.get("checks")
        if not isinstance(checks, dict):
            continue
        for check_name, status in checks.items():
            _append_invalid_status(
                invalid,
                _StatusValidationRequest(
                    value=status,
                    field=f"coverage.targets[{target_index}].checks.{check_name}",
                    target_id=target_id,
                    check_name=str(check_name),
                    allow_empty=False,
                ),
            )
    return invalid


def _append_invalid_status(invalid: list[dict[str, str]], request: _StatusValidationRequest) -> None:
    raw_status = str(request.value or "").strip()
    if not raw_status and request.allow_empty:
        return
    if raw_status in TASK_PROGRESS_KNOWN_STATUSES:
        return
    invalid.append(
        {
            "field": request.field,
            "id": request.target_id,
            "check": request.check_name,
            "status": raw_status,
        }
    )


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
        if task_progress_status_is_closed(item.get("status"))
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
    if normalized.get("expected_outputs"):
        summary["expected_outputs"] = normalized["expected_outputs"]
    if normalized.get("load_error"):
        summary["load_error"] = normalized["load_error"]
    return summary


def normalize_task_progress(payload: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    items = [_normalize_item(item) for item in _list(payload.get("items"))]
    coverage = normalize_coverage(payload)
    expected_outputs = normalize_expected_outputs(payload)
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
    if expected_outputs:
        normalized["expected_outputs"] = expected_outputs
    load_error = payload.get("load_error") or _first_load_error(payload.get("load_errors"))
    if isinstance(load_error, dict):
        normalized["load_error"] = load_error
    ref = str(payload.get("ref") or "").strip()
    if ref:
        normalized["ref"] = ref
    return normalized


# LLM: 交付产物声明的归一化(产物类型/数量对账门的声明侧,实锤来源 R6b/R6c:
#   prompt 要求 24 周/每篇一个 PDF,实交 1 个 md/0 个 PDF,closeout 只查"有产物"
#   照样 ok=true)。开放世界 schema:每条 {pattern, min_count, note}——pattern 是
#   相对任务交付目录的文件名或 glob(也接受绝对路径),扩展名天然携带类型;
#   min_count 是该 pattern 至少应存在的文件数(默认 1)。同 pattern 去重后者覆盖;
#   含 ".." 段或空 pattern 的条目丢弃(防路径逃逸,不报错不崩)。
# 函数用途: 把模型声明的"本任务最终应交付什么文件"清洗成干净清单。
def normalize_expected_outputs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    order: list[str] = []
    by_pattern: dict[str, dict[str, Any]] = {}
    for raw in _list(payload.get("expected_outputs")):
        entry = _normalize_expected_output_entry(raw)
        if not entry:
            continue
        pattern = str(entry["pattern"])
        if pattern not in by_pattern:
            order.append(pattern)
        by_pattern[pattern] = entry
    return [by_pattern[pattern] for pattern in order]


# 函数用途: 清洗单条产物声明,坏条目(空/越界/坏数量)返回空 dict 丢弃。
def _normalize_expected_output_entry(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = {"pattern": value}
    if not isinstance(value, dict):
        return {}
    pattern = str(value.get("pattern") or value.get("path") or "").strip()
    if not pattern or ".." in Path(pattern).parts:
        return {}
    try:
        min_count = int(value.get("min_count") or 1)
    except (TypeError, ValueError):
        min_count = 1
    entry: dict[str, Any] = {"pattern": pattern, "min_count": max(1, min_count)}
    note = str(value.get("note") or "").strip()
    if note:
        entry["note"] = note
    return entry


def merge_task_progress(existing: dict[str, Any], update: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    base = normalize_task_progress(existing, run_id=run_id)
    merged_items = _merge_items(base["items"], [_normalize_item(item) for item in _list(update.get("items"))])
    coverage = merge_coverage(base.get("coverage", {}), coverage_from_update(update))
    expected_outputs = _merge_expected_outputs(
        list(base.get("expected_outputs") or []),
        normalize_expected_outputs(update),
    )
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
    if hints["messages"]:
        payload["quality_hints"] = hints
    if coverage["targets"] or coverage["goal"] or coverage["dimensions"]:
        payload["coverage"] = coverage
    if expected_outputs:
        payload["expected_outputs"] = expected_outputs
    return payload


# 函数用途: 合并产物声明:同 pattern 新声明覆盖旧值(声明是"当前认知的要求"),
#   新 pattern 追加,既有声明不会因一次不带 expected_outputs 的更新而丢失。
def _merge_expected_outputs(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    order = [str(entry.get("pattern") or "") for entry in existing]
    by_pattern = {str(entry.get("pattern") or ""): dict(entry) for entry in existing}
    for entry in incoming:
        pattern = str(entry.get("pattern") or "")
        if pattern not in by_pattern:
            order.append(pattern)
        by_pattern[pattern] = dict(entry)
    return [by_pattern[pattern] for pattern in order if pattern]


def normalize_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    raw_coverage = payload.get("coverage")
    coverage = dict(raw_coverage) if isinstance(raw_coverage, dict) else {}
    dimensions = string_list(coverage.get("dimensions"))
    normalized_targets = [
        target for item in _list(coverage.get("targets"))
        if (target := _normalize_coverage_target(item))
    ]
    normalized = {
        "goal": str(coverage.get("goal") or "").strip(),
        "dimensions": dimensions,
        "targets": normalized_targets,
    }
    requirement = str(coverage.get("coverage_requirement") or "").strip()
    if requirement:
        normalized["coverage_requirement"] = requirement
    enforcement = str(coverage.get("enforcement") or "").strip()
    if enforcement:
        normalized["enforcement"] = enforcement
    normalized["counts"] = _coverage_counts(normalized_targets)
    return normalized


def coverage_from_update(update: dict[str, Any]) -> dict[str, Any]:
    return normalize_coverage(update)


def merge_coverage(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    existing = normalize_coverage({"coverage": existing})
    incoming = normalize_coverage({"coverage": incoming})
    targets = _merge_coverage_targets(existing["targets"], incoming["targets"])
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
            "evidence": dedupe_strings([*string_list(previous.get("evidence")), *string_list(item.get("evidence"))]),
        }
        if _coverage_target_done(previous) and not _coverage_target_done(item):
            by_id[item_id]["status"] = previous.get("status") or "done"
            by_id[item_id]["checks"] = _preserve_done_checks(previous, by_id[item_id])
    return [by_id[item_id] for item_id in order if item_id in by_id]


def _preserve_done_checks(previous: dict[str, Any], merged: dict[str, Any]) -> dict[str, str]:
    checks = dict(merged.get("checks") or {})
    for key, status in dict(previous.get("checks") or {}).items():
        if task_progress_status_is_closed(status):
            checks[key] = str(status)
    return checks


def _normalize_coverage_target(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    item = dict(value)
    target_id = str(item.get("id") or "").strip()
    title = str(item.get("title") or target_id).strip()
    raw_status = str(item.get("status") or "").strip()
    result = {
        "id": target_id or _safe_id(title) or "target",
        "title": title,
        "status": normalize_task_progress_status(raw_status or "pending"),
        "checks": _normalize_target_checks(item),
        "evidence": string_list(item.get("evidence")),
        "notes": str(item.get("notes") or "").strip(),
        "next": str(item.get("next") or "").strip(),
    }
    if raw_status and not task_progress_status_is_known(raw_status):
        result["raw_status"] = raw_status
        result["status_protocol_error"] = TASK_PROGRESS_STATUS_INVALID
    for key in ("owner", "priority", "updated_at", "coverage_kind", "source_ref"):
        if key in item:
            result[key] = item[key]
    return result


def _normalize_target_checks(item: dict[str, Any]) -> dict[str, str]:
    return _normalize_checks(item.get("checks"))


def _normalize_checks(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip(): normalize_task_progress_status(str(status or "").strip() or "pending")
        for key, status in value.items()
        if str(key).strip()
    }


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
        "checks_done": sum(1 for status in checks if task_progress_status_is_closed(status)),
        "checks_incomplete": sum(1 for status in checks if not task_progress_status_is_closed(status)),
    }


def _coverage_target_done(target: dict[str, Any]) -> bool:
    checks = dict(target.get("checks") or {})
    if checks:
        return all(task_progress_status_is_closed(status) for status in checks.values())
    return task_progress_status_is_closed(target.get("status"))


def quality_hints(
    items: list[dict[str, Any]],
    *,
    incoming: list[dict[str, Any]] | None = None,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    coverage = normalize_coverage({"coverage": coverage or {}})
    result_without_evidence = _items_with_result_without_evidence(items)
    messages = _result_messages(result_without_evidence)
    if incoming:
        messages.extend(_incoming_messages(incoming))
        incoming_coverage = normalize_coverage({"coverage": {"targets": incoming}})
        if incoming_coverage["targets"] and not _has_explicit_coverage(coverage):
            coverage = incoming_coverage
    coverage_done_without_evidence = _coverage_done_without_evidence(coverage)
    coverage_incomplete = _coverage_incomplete(coverage)
    messages.extend(_coverage_messages(coverage_done_without_evidence, coverage_incomplete))
    next_suggestions = _next_suggestions(
        result_without_evidence=result_without_evidence,
        coverage_done_without_evidence=coverage_done_without_evidence,
        coverage_incomplete=coverage_incomplete,
    )
    return {
        "severity": "soft",
        "result_without_evidence_count": len(result_without_evidence),
        "result_without_evidence_ids": result_without_evidence[:20],
        "done_without_evidence_count": len(result_without_evidence),
        "done_without_evidence_ids": result_without_evidence[:20],
        "coverage_done_without_evidence_count": len(coverage_done_without_evidence),
        "coverage_done_without_evidence_ids": coverage_done_without_evidence[:20],
        "coverage_incomplete_count": len(coverage_incomplete),
        "coverage_incomplete_ids": coverage_incomplete[:20],
        "next_suggestions": next_suggestions,
        "soft_prompt": _soft_prompt(next_suggestions),
        "messages": dedupe_strings(messages),
    }


def _items_with_result_without_evidence(items: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("id") or "")
        for item in items
        if _has_result_signal(item) and not string_list(item.get("evidence"))
    ]


def _coverage_done_without_evidence(coverage: dict[str, Any]) -> list[str]:
    return [
        str(target.get("id") or "")
        for target in coverage.get("targets", [])
        if _coverage_target_done(target) and not string_list(target.get("evidence"))
    ]


def _coverage_incomplete(coverage: dict[str, Any]) -> list[str]:
    return [
        str(target.get("id") or "")
        for target in coverage.get("targets", [])
        if not _coverage_target_done(target)
    ]


def _result_messages(result_without_evidence: list[str]) -> list[str]:
    if not result_without_evidence:
        return []
    return [
        "有些条目已经写了状态、结果或结论，但没有 evidence。建议补上看过的文件、产物路径、工具结果或简短证据引用；这只是软提醒，不会阻断任务。"
    ]


def _incoming_messages(incoming: list[dict[str, Any]]) -> list[str]:
    batch_result_without_evidence = [
        str(item.get("id") or "")
        for item in incoming
        if _has_result_signal(item) and not string_list(item.get("evidence"))
    ]
    if len(batch_result_without_evidence) < 3:
        return []
    return ["这次一次性写了多项状态、结果或结论，但缺少 evidence。长任务更稳的做法是边读、边分析、边写报告时同步更新进度和证据。"]


def _coverage_messages(done_without_evidence: list[str], incomplete: list[str]) -> list[str]:
    messages: list[str] = []
    if done_without_evidence:
        messages.append(
            "覆盖清单里有对象看起来已完成，但缺少 evidence。建议补上读过的文件、资料来源或写入报告的位置；这只是软提醒，不会阻断任务。"
        )
    if incomplete:
        messages.append(
            "覆盖清单里还有对象没有逐项完成。建议继续补未完成对象；先读取或核对对应来源，记录证据，再把结论写进产物。"
        )
    return messages


def _has_explicit_coverage(coverage: dict[str, Any]) -> bool:
    return bool(
        coverage.get("goal")
        or coverage.get("dimensions")
        or coverage.get("targets")
    )


def _has_result_signal(item: dict[str, Any]) -> bool:
    return any(str(item.get(key) or "").strip() for key in _RESULT_FIELDS)


def _next_suggestions(
    *,
    result_without_evidence: list[str],
    coverage_done_without_evidence: list[str],
    coverage_incomplete: list[str],
) -> list[str]:
    suggestions: list[str] = []
    if coverage_incomplete:
        suggestions.append("继续补未完成对象：先选一个未完成对象，读取或核对对应来源，再更新 checks/evidence。")
    if result_without_evidence or coverage_done_without_evidence:
        suggestions.append("补证据引用：不要只打勾；每个有状态、结果或结论的条目最好写一个文件路径、产物路径、工具结果或来源说明。")
    if coverage_incomplete or result_without_evidence or coverage_done_without_evidence:
        suggestions.append("写报告时同步推进账本：读过什么、分析了什么、写进报告哪里，都用 task_progress 轻量记录。")
    return dedupe_strings(suggestions)


def _soft_prompt(suggestions: list[str]) -> str:
    if not suggestions:
        return ""
    return "软提醒，不会阻断任务：" + "；".join(suggestions)


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
    raw_status = str(item.get("status") or "").strip()
    stored_raw_status = str(item.get("raw_status") or "").strip()
    status = normalize_task_progress_status(raw_status or "pending")
    result = {
        "id": item_id or _safe_id(title) or "item",
        "title": title,
        "status": status,
        "notes": str(item.get("notes") or "").strip(),
        "next": str(item.get("next") or "").strip(),
        "evidence": string_list(item.get("evidence")),
    }
    raw_status_for_metadata = stored_raw_status or raw_status
    if raw_status_for_metadata and not task_progress_status_is_known(raw_status_for_metadata):
        result["raw_status"] = raw_status_for_metadata
        result["status_protocol_error"] = TASK_PROGRESS_STATUS_INVALID
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
    return [by_id[item_id] for item_id in order if item_id in by_id]


def _merge_item_overlay(previous: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = {**previous, **{key: value for key, value in incoming.items() if value not in ("", [], None)}}
    if "status" in incoming and "raw_status" not in incoming:
        merged.pop("raw_status", None)
        merged.pop("status_protocol_error", None)
    if incoming.get("evidence") or previous.get("evidence"):
        merged["evidence"] = dedupe_strings([*string_list(previous.get("evidence")), *string_list(incoming.get("evidence"))])
    return merged


def _should_preserve_done_facts(previous: dict[str, Any], incoming: dict[str, Any]) -> bool:
    return (
        task_progress_status_is_closed(previous.get("status"))
        and not any(_truthy(incoming.get(key)) for key in _EXPLICIT_OVERWRITE_KEYS)
    )


def _merge_done_item_without_overwriting_facts(previous: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = _merge_item_overlay(previous, incoming)
    for key in _FACT_FIELDS:
        if previous.get(key) not in ("", [], None):
            merged[key] = previous[key]
    merged["evidence"] = dedupe_strings([*string_list(previous.get("evidence")), *string_list(incoming.get("evidence"))])
    return merged


def _summary_item(item: dict[str, Any], *, include_facts: bool = False) -> dict[str, Any]:
    summary = {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "status": str(item.get("status") or ""),
        "next": str(item.get("next") or ""),
    }
    if include_facts:
        summary.update(_summary_item_facts(item))
    return summary


def _summary_item_facts(item: dict[str, Any]) -> dict[str, Any]:
    facts = {
        key: text
        for key in ("notes", "result", "outcome", "conclusion", "decision", "summary")
        if (text := str(item.get(key) or "").strip())
    }
    evidence = string_list(item.get("evidence"))[:8]
    if evidence:
        facts["evidence"] = evidence
    return facts


def task_progress_status_is_closed(value: object) -> bool:
    return normalize_task_progress_status(value) in TASK_PROGRESS_CLOSED_STATUSES


def task_progress_status_is_done(value: object) -> bool:
    return normalize_task_progress_status(value) == "done"


def normalize_task_progress_status(value: object) -> str:
    text = str(value or "").strip()
    return text if task_progress_status_is_known(text) else TASK_PROGRESS_UNKNOWN_STATUS


def task_progress_status_is_known(value: object) -> bool:
    return str(value or "").strip() in TASK_PROGRESS_KNOWN_STATUSES


def _merge_key(item: dict[str, Any]) -> str:
    return str(item.get("id") or "").strip()


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true"}


def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {"total": len(items)}
    for item in items:
        status = str(item.get("status") or "pending").strip() or "pending"
        key = status if status in TASK_PROGRESS_COUNT_STATUSES else "other"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _list(value: object) -> list:
    return list(value) if isinstance(value, list | tuple) else []


def _safe_id(value: str) -> str:
    # 体检收敛:全仓 safe id 唯一权威在 common/safe_id(此处保留薄转发,
    # 调用点多且语义=默认 main)。
    return safe_id(value, default="main")


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
    "normalize_expected_outputs",
    "normalize_task_progress",
    "progress_path",
    "read_task_progress",
    "read_task_progress_report",
    "task_progress_summary",
    "invalid_item_statuses",
    "invalid_coverage_statuses",
    "write_task_progress",
]
