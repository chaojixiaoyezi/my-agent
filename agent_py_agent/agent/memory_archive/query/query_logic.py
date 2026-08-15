
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...common.opaque_id import OpaqueIdError
from ...user_space.home_runtime_query import home_task_workspace_payload
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
from .resume_guidance import ResumeGuidanceRequest, build_resume_guidance

__all__ = [
    "ArchiveFilterOptions",
    "ArchiveQueryRequest",
    "ArchiveQueryResponse",
    "CollectArchiveRecordsParams",
    "FilterArchiveRecordsParams",
    "RawArchiveCollectOptions",
    "ResumeContext",
    "ResumeGuidanceRequest",
    "apply_filters",
    "archive_filters_from_args",
    "build_resume_guidance",
    "collect_archive_records",
    "collect_gateway_payloads",
    "collect_raw_archive_records",
    "collect_resume_task_ids",
    "collect_task_payloads",
    "evaluate_filters",
    "execute_archive_query",
    "filter_by_fields",
    "filter_by_level",
    "filter_by_query_text",
    "filter_by_time_window",
    "filter_archive_records",
    "local_hit_payload",
    "paginate_records",
    "resume_local_query",
    "strip_sort_keys",
    "task_recovery_read_paths",
]


@dataclass(frozen=True)
class CollectArchiveRecordsParams:
    layer: str
    date_key: str | None = None
    limit: int = 0
    level: int | None = None
    file_limit: int = 30


@dataclass(frozen=True)
class FilterArchiveRecordsParams:
    query: str = ""
    filters: dict[str, str] | None = None
    since: str | None = None
    until: str | None = None
    level: int | None = None


@dataclass(frozen=True)
class ArchiveFilterOptions:
    query: str
    filters: dict[str, str]
    since: str | None
    until: str | None
    level: int | None = None


@dataclass
class ArchiveQueryRequest:
    """normalized query parameters for archive search."""

    query: str = ""
    since: str | None = None
    until: str | None = None
    level: int | None = None
    layer: str = "all"
    date_key: str | None = None
    limit: int = 100
    filters: dict[str, str] = field(default_factory=dict)


@dataclass
class ArchiveQueryResponse:
    """normalized archive search response with pagination info."""

    records: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 100
    has_more: bool = False

    @property
    def pages(self) -> int:
        """Return total number of pages."""
        if self.page_size <= 0:
            return 1
        return (self.total + self.page_size - 1) // self.page_size


@dataclass(frozen=True)
class RawArchiveCollectOptions:
    layer: str
    date_key: str | None
    limit: int
    level: int | None = None


@dataclass
class ResumeContext:
    """context object for memory resume operations."""

    archive_matches: list[dict[str, Any]] = field(default_factory=list)
    local_hits: list[dict[str, Any]] = field(default_factory=list)
    task_payloads: list[dict[str, Any]] = field(default_factory=list)
    gateway_payloads: list[dict[str, Any]] = field(default_factory=list)
    guidance: dict[str, Any] = field(default_factory=dict)


def execute_archive_query(
    root: Path,
    request: ArchiveQueryRequest,
) -> ArchiveQueryResponse:

    records = collect_raw_archive_records(
        root,
        RawArchiveCollectOptions(request.layer, request.date_key, request.limit, request.level),
    )
    filtered = apply_filters(
        records,
        ArchiveFilterOptions(
            query=request.query,
            filters=request.filters,
            since=request.since,
            until=request.until,
            level=request.level,
        ),
    )
    return paginate_records(
        filtered,
        page=request.page if hasattr(request, "page") else 1,
        page_size=request.page_size if hasattr(request, "page_size") else 100,
    )


def collect_raw_archive_records(
    root: Path,
    options: RawArchiveCollectOptions,
) -> list[dict[str, Any]]:
    return collect_archive_records(
        root,
        params=CollectArchiveRecordsParams(
            layer=options.layer,
            date_key=options.date_key,
            limit=options.limit,
            level=options.level,
        ),
    )


def apply_filters(
    records: list[dict[str, Any]],
    options: ArchiveFilterOptions,
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if evaluate_filters(record, options=options)
    ]


def paginate_records(
    records: list[dict[str, Any]],
    *,
    page: int = 1,
    page_size: int = 100,
) -> ArchiveQueryResponse:

    total = len(records)
    start = (page - 1) * page_size
    end = start + page_size
    page_records = records[start:end]
    return ArchiveQueryResponse(
        records=page_records,
        total=total,
        page=page,
        page_size=page_size,
        has_more=end < total,
    )


def collect_archive_records(
    root: Path,
    *,
    layer: str = "all",
    date_key: str | None = None,
    limit: int = 0,
    level: int | None = None,
    file_limit: int = 30,
    params: CollectArchiveRecordsParams | None = None,
) -> list[dict[str, Any]]:
    values = params or CollectArchiveRecordsParams(layer, date_key, limit, level, file_limit)
    layer = str(values.layer)
    limit = int(values.limit)
    records: list[dict[str, Any]] = []
    for current_layer, path in _archive_files(
        root,
        layer=layer,
        date_key=values.date_key,
        file_limit=values.file_limit,
    ):
        records.extend(_read_archive_file(current_layer, path))
    if values.level is not None:
        records = [record for record in records if int(record.get("archive_level", -1)) == int(values.level)]
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
    *,
    params: FilterArchiveRecordsParams | None = None,
    query: str = "",
    filters: dict[str, str] | None = None,
    since: str | None = None,
    until: str | None = None,
    level: int | None = None,
) -> list[dict[str, Any]]:
    values = params or FilterArchiveRecordsParams(query, filters, since, until, level)
    return apply_filters(
        records,
        ArchiveFilterOptions(
            query=str(values.query),
            filters=dict(values.filters or {}),
            since=values.since,
            until=values.until,
            level=values.level,
        ),
    )


def filter_by_fields(record: dict[str, Any], filters: dict[str, str]) -> bool:

    for field_name, value in filters.items():
        if str(record.get(field_name, "")) != value:
            return False
    return True


def filter_by_level(record: dict[str, Any], level: int | None) -> bool:

    if level is None:
        return True
    return int(record.get("archive_level", -1)) == int(level)


def filter_by_time_window(
    record: dict[str, Any],
    since_ts: float | None,
    until_ts: float | None,
) -> bool:

    created_at = float(record.get("created_at_sort", 0.0) or 0.0)
    if since_ts is not None and created_at < since_ts:
        return False
    if until_ts is not None and created_at > until_ts:
        return False
    return True


def filter_by_query_text(record: dict[str, Any], query_text: str) -> bool:

    if not query_text:
        return True
    return query_text in _archive_search_text(record)


def evaluate_filters(
    record: dict[str, Any],
    options: ArchiveFilterOptions,
) -> bool:
    if not filter_by_fields(record, options.filters):
        return False
    if not filter_by_level(record, options.level):
        return False

    since_ts = _created_at_sort(options.since or "", default=0.0) if options.since else None
    until_ts = _created_at_sort(options.until or "", default=0.0) if options.until else None
    if until_ts is not None and _is_date_only(options.until or ""):
        until_ts += 86399.999999

    if not filter_by_time_window(record, since_ts, until_ts):
        return False
    if not filter_by_query_text(record, options.query.strip().lower()):
        return False
    return True

def resume_local_query(args, archive_matches: list[dict[str, Any]]) -> str:
    for value in (args.query, args.run_id, args.request_id, args.session_id, args.task_id):
        text = str(value or "").strip()
        if text:
            return text
    for record in archive_matches:
        if text := _first_record_id(record):
            return text
    return ""

def local_hit_payload(hit, *, preview_chars: int = 500) -> dict[str, Any]:
    return {
        "id": hit.id,
        "source_type": hit.source_type,
        "source_id": hit.source_id,
        "title": hit.title,
        "content_preview": hit.content[: max(0, int(preview_chars))],
        "metadata": hit.metadata,
        "visibility": hit.visibility,
        "updated_at": hit.updated_at,
        "content_path": hit.content_path,
    }

def collect_resume_task_ids(args, archive_matches: list[dict[str, Any]], local_hits: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for value in (args.run_id, args.task_id):
        _append_resume_task_ref(ids, value)
    for record in archive_matches:
        _append_archive_task_ids(ids, record)
    for hit in local_hits:
        _append_local_hit_task_ids(ids, hit)
    return ids


def _append_resume_task_ref(ids: list[str], value: object) -> None:
    text = str(value or "").strip()
    if text and text not in ids:
        ids.append(text)


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

def strip_sort_keys(payload: Any) -> Any:
    if isinstance(payload, list):
        return [strip_sort_keys(item) for item in payload]
    if isinstance(payload, dict):
        return {key: strip_sort_keys(value) for key, value in payload.items() if key != "created_at_sort"}
    return payload

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
    metadata = hit.get("metadata", {}) if isinstance(hit.get("metadata"), dict) else {}
    _append_run_id(ids, metadata.get("run_id"))
    _append_run_id(ids, metadata.get("task_id"))

def _task_payload(agent, run_id: str) -> dict[str, Any]:
    try:
        task = agent.subagents.load(run_id)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return _missing_or_home_task_payload(agent, run_id)
    except OpaqueIdError:
        # 3.txt B.2：非 opaque run_id（中文任务名、旧格式带空格 ID）不是
        # 框架 run 记录，而是用户可读任务引用——本查询的 home task workspace
        # 通道正是按 task_name/task_id 字段匹配，直接转入，不当作子代理 run。
        return _missing_or_home_task_payload(agent, run_id)
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


def _missing_or_home_task_payload(agent, run_id: str) -> dict[str, Any]:
    payload = home_task_workspace_payload(agent.home_paths, run_id)
    if payload is not None:
        return payload
    return {"run_id": run_id, "exists": False, "error": "task not found"}


def task_recovery_read_paths(task: Any) -> list[str]:
    """Return compact-first task fact sources for resume."""

    return _dedupe_strings(
        [
            getattr(task, "checkpoint_json", ""),
            getattr(task, "status_report_json", ""),
            getattr(task, "progress_md", ""),
            getattr(task, "decision_ledger_json", ""),
            getattr(task, "failing_tests_json", ""),
            getattr(task, "next_actions_json", ""),
            getattr(task, "status_file", ""),
            getattr(task, "work_log_file", ""),
            getattr(task, "handoff_file", ""),
            getattr(task, "acceptance_file", ""),
            getattr(task, "test_checklist_file", ""),
            getattr(task, "output_json", ""),
        ]
    )


def _task_read_paths(task) -> list[str]:
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
