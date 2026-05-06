from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive_helpers import (
    _append_run_id,
    _archive_search_text,
    _created_at_sort,
    _dedupe_strings,
    _is_date_only,
)
from .archive_io import (
    _archive_files,
    _gateway_terminal_request_path,
    _read_archive_file,
)
from .task_sources import task_recovery_read_paths


@dataclass(frozen=True)
class _ArchiveFilterContext:
    query_text: str
    filters: dict[str, str]
    since_ts: float | None
    until_ts: float | None
    level: int | None

def collect_archive_records(
    root: Path,
    **options: Any,
) -> list[dict[str, Any]]:
    layer = str(options["layer"])
    date_key = options.get("date_key")
    limit = int(options["limit"])
    level = options.get("level")
    records: list[dict[str, Any]] = []
    for current_layer, path in _archive_files(root, layer=layer, date_key=date_key):
        records.extend(_read_archive_file(current_layer, path))
    if level is not None:
        records = [record for record in records if int(record.get("archive_level", -1)) == int(level)]
    records.sort(key=lambda item: (item["created_at_sort"], item["file_path"], item["line_no"]), reverse=True)
    return records[:limit] if limit > 0 else records

def archive_filters_from_args(args) -> dict[str, str]:
    fields = [
        "session_id",
        "request_id",
        "run_id",
        "task_id",
        "speaker",
        "target",
        "action",
        "status",
        "tool_name",
        "source",
    ]
    return {
        field: str(getattr(args, field, "") or "").strip()
        for field in fields
        if str(getattr(args, field, "") or "").strip()
    }

def filter_archive_records(
    records: list[dict[str, Any]],
    **options: Any,
) -> list[dict[str, Any]]:
    query = str(options.get("query", ""))
    filters = dict(options.get("filters", {}) or {})
    since = options.get("since")
    until = options.get("until")
    level = options.get("level")
    query_text = query.strip().lower()
    context = _ArchiveFilterContext(
        query_text=query_text,
        filters=filters,
        since_ts=_created_at_sort(since or "", fallback=0.0) if since else None,
        until_ts=_until_timestamp(until),
        level=level,
    )
    return [
        record
        for record in records
        if _archive_record_matches(record, context)
    ]

def resume_local_query(args, archive_matches: list[dict[str, Any]]) -> str:
    for value in (args.query, args.run_id, args.request_id, args.session_id, args.task_id):
        text = str(value or "").strip()
        if text:
            return text
    for record in archive_matches:
        if text := _first_record_id(record):
            return text
    return ""

def local_hit_payload(hit) -> dict[str, Any]:
    return {
        "id": hit.id,
        "source_type": hit.source_type,
        "source_id": hit.source_id,
        "title": hit.title,
        "content_preview": hit.content[:500],
        "metadata": hit.metadata,
        "visibility": hit.visibility,
        "updated_at": hit.updated_at,
        "content_path": hit.content_path,
    }

def collect_resume_task_ids(args, archive_matches: list[dict[str, Any]], local_hits: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for value in (args.run_id, args.task_id):
        _append_run_id(ids, value)
    for record in archive_matches:
        _append_archive_task_ids(ids, record)
    for hit in local_hits:
        _append_local_hit_task_ids(ids, hit)
    return ids

def collect_task_payloads(agent, task_ids: list[str], *, limit: int) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for run_id in task_ids[:limit]:
        payloads.append(_task_payload(agent, run_id))
    return payloads

def collect_gateway_payloads(local_hits: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for hit in local_hits:
        payload = _gateway_payload(hit)
        if not payload:
            continue
        payloads.append(payload)
        if len(payloads) >= max(limit, 0):
            break
    return payloads

def build_resume_guidance(
    archive_matches: list[dict[str, Any]],
    local_hits: list[dict[str, Any]],
    task_payloads: list[dict[str, Any]],
    gateway_payloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    gateway_items = gateway_payloads or []
    return {
        "archive_match_count": len(archive_matches),
        "local_match_count": len(local_hits),
        "task_fact_source_count": len(task_payloads),
        "gateway_fact_source_count": len(gateway_items),
        "recommended_read_paths": _recommended_resume_reads(task_payloads, gateway_items, local_hits)[:20],
        "next_actions": _resume_next_actions(task_payloads, gateway_items),
    }

def strip_sort_keys(payload: Any) -> Any:
    if isinstance(payload, list):
        return [strip_sort_keys(item) for item in payload]
    if isinstance(payload, dict):
        return {key: strip_sort_keys(value) for key, value in payload.items() if key != "created_at_sort"}
    return payload

def _until_timestamp(until: str | None) -> float | None:
    if not until:
        return None
    timestamp = _created_at_sort(until, fallback=0.0)
    if _is_date_only(until):
        timestamp += 86399.999999
    return timestamp

def _archive_record_matches(
    record: dict[str, Any],
    context: _ArchiveFilterContext,
) -> bool:
    created_at = float(record.get("created_at_sort", 0.0) or 0.0)
    return (
        _matches_filters(record, context.filters)
        and _matches_level(record, context.level)
        and (context.since_ts is None or created_at >= context.since_ts)
        and (context.until_ts is None or created_at <= context.until_ts)
        and (not context.query_text or context.query_text in _archive_search_text(record))
    )

def _matches_filters(record: dict[str, Any], filters: dict[str, str]) -> bool:
    return all(str(record.get(field, "")) == value for field, value in filters.items())

def _matches_level(record: dict[str, Any], level: int | None) -> bool:
    return level is None or int(record.get("archive_level", -1)) == int(level)

def _first_record_id(record: dict[str, Any]) -> str:
    for field in ("run_id", "task_id", "request_id", "session_id"):
        text = str(record.get(field, "") or "").strip()
        if text:
            return text
    return ""

def _append_archive_task_ids(ids: list[str], record: dict[str, Any]) -> None:
    _append_run_id(ids, record.get("run_id"))
    _append_run_id(ids, record.get("task_id"))
    for ref in record.get("task_refs", []) or []:
        _append_run_id(ids, ref)

def _append_local_hit_task_ids(ids: list[str], hit: dict[str, Any]) -> None:
    if str(hit.get("source_type", "")).startswith("subagent"):
        _append_run_id(ids, hit.get("source_id"))
    metadata = hit.get("metadata", {}) if isinstance(hit.get("metadata"), dict) else {}
    _append_run_id(ids, metadata.get("run_id"))
    _append_run_id(ids, metadata.get("task_id"))

def _task_payload(agent, run_id: str) -> dict[str, Any]:
    try:
        task = agent.subagents.load(run_id)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return {"run_id": run_id, "exists": False, "error": "task not found"}
    paths = _task_read_paths(task)
    return {
        "run_id": task.id,
        "exists": True,
        "status": task.status,
        "verification_status": task.verification_status,
        "goal": task.goal,
        "updated_at": task.updated_at,
        "task_dir": task.task_dir,
        "recommended_read_paths": paths,
        "authority_validation": _validate_task_fact_sources(paths),
    }

def _task_read_paths(task) -> list[str]:
    # LLM: keep legacy query_logic callers aligned with query_service task sources.
    return task_recovery_read_paths(task)

def _validate_task_fact_sources(paths: list[str]) -> dict[str, Any]:
    missing = [path for path in paths if path and not Path(path).exists()]
    return {"ok": not missing, "missing_paths": missing}

def _gateway_payload(hit: dict[str, Any]) -> dict[str, Any] | None:
    source_type = str(hit.get("source_type", "") or "")
    source_id = str(hit.get("source_id", "") or "")
    metadata = hit.get("metadata", {}) if isinstance(hit.get("metadata"), dict) else {}
    request_id = str(metadata.get("request_id") or source_id or "").strip()
    if source_type != "gateway_request" and not request_id.startswith("gwreq-"):
        return None
    request_path = _gateway_terminal_request_path(str(metadata.get("request_path", "") or "").strip())
    response_path = str(metadata.get("response_path", "") or "").strip()
    content_path = str(hit.get("content_path", "") or "").strip()
    return {
        "request_id": request_id,
        "source_id": source_id,
        "status": str(metadata.get("status", "") or ""),
        "ok": bool(metadata.get("ok", False)),
        "request_path": request_path,
        "response_path": response_path,
        "content_path": content_path,
        "recommended_read_paths": _dedupe_strings([request_path, response_path, content_path]),
    }

def _recommended_resume_reads(
    task_payloads: list[dict[str, Any]],
    gateway_payloads: list[dict[str, Any]],
    local_hits: list[dict[str, Any]],
) -> list[str]:
    recommended_reads: list[str] = []
    for payload in (*task_payloads, *gateway_payloads):
        _append_paths(recommended_reads, payload.get("recommended_read_paths", []) or [])
    _append_paths(recommended_reads, (str(hit.get("content_path", "") or "") for hit in local_hits))
    return recommended_reads

def _append_paths(target: list[str], paths) -> None:
    for path in paths:
        if path and path not in target:
            target.append(path)

def _resume_next_actions(
    task_payloads: list[dict[str, Any]],
    gateway_payloads: list[dict[str, Any]],
) -> list[str]:
    next_actions = [
        "Read task fact sources before deciding whether work can continue.",
        "Treat archive matches as recovery clues, not final authority.",
    ]
    invalid_authority = _invalid_authority_ids(task_payloads)
    if invalid_authority:
        next_actions.append("Repair missing task authority files before resume: " + ", ".join(invalid_authority[:5]))
    if not task_payloads and not gateway_payloads:
        next_actions.append("Use memory-archive-search to narrow request_id/run_id/session_id first.")
    return next_actions

def _invalid_authority_ids(task_payloads: list[dict[str, Any]]) -> list[str]:
    return [
        task.get("run_id", "")
        for task in task_payloads
        if isinstance(task.get("authority_validation"), dict) and not task["authority_validation"].get("ok", False)
    ]
