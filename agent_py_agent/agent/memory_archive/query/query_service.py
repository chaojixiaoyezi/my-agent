from __future__ import annotations

"""LLM: query execution and filter application for memory archive.

新手说明:
这个文件放的是查询执行逻辑——收集归档记录、应用过滤器、管理分页。
它依赖 archive_io 读写文件，依赖 filter_policy 做过滤判断。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archive_helpers import _append_run_id, _dedupe_strings
from .archive_io import _archive_files, _gateway_terminal_request_path, _read_archive_file
from .filter_policy import ArchiveFilterOptions, evaluate_filters
from .query_models import ArchiveQueryRequest, ArchiveQueryResponse, paginate_records


@dataclass(frozen=True)
class RawArchiveCollectOptions:
    layer: str
    date_key: str | None
    limit: int
    level: int | None = None


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
    options: RawArchiveCollectOptions | str | None = None,
    *args: Any,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    if not isinstance(options, RawArchiveCollectOptions):
        options = RawArchiveCollectOptions(
            layer=str(options or kwargs["layer"]),
            date_key=args[0] if len(args) > 0 else kwargs.get("date_key"),
            limit=int(args[1] if len(args) > 1 else kwargs.get("limit", 0)),
            level=args[2] if len(args) > 2 else kwargs.get("level"),
        )

    records: list[dict[str, Any]] = []
    for current_layer, path in _archive_files(root, layer=options.layer, date_key=options.date_key):
        records.extend(_read_archive_file(current_layer, path))
    if options.level is not None:
        records = [r for r in records if int(r.get("archive_level", -1)) == int(options.level)]
    records.sort(key=lambda item: (item["created_at_sort"], item["file_path"], item["line_no"]), reverse=True)
    return records[:options.limit] if options.limit > 0 else records


def apply_filters(
    records: list[dict[str, Any]],
    options: ArchiveFilterOptions | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    options = options or ArchiveFilterOptions(
        query=str(kwargs.get("query", "")),
        filters=dict(kwargs.get("filters", {}) or {}),
        since=kwargs.get("since"),
        until=kwargs.get("until"),
        level=kwargs.get("level"),
    )

    return [
        record
        for record in records
        if evaluate_filters(record, options=options)
    ]


def collect_task_payloads(agent, task_ids: list[str], *, limit: int) -> list[dict[str, Any]]:

    import json

    payloads: list[dict[str, Any]] = []
    for run_id in task_ids[:limit]:
        try:
            task = agent.subagents.load(run_id)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            payloads.append({"run_id": run_id, "exists": False, "error": "task not found"})
            continue
        payloads.append({
            "run_id": task.id, "exists": True, "status": task.status,
            "verification_status": task.verification_status, "goal": task.goal,
            "updated_at": task.updated_at, "task_dir": task.task_dir,
            "recommended_read_paths": [
                task.status_file, task.work_log_file, task.handoff_file,
                task.acceptance_file, task.test_checklist_file, task.output_json,
            ],
            "authority_validation": _validate_task_fact_sources([
                task.status_file, task.work_log_file, task.handoff_file,
                task.acceptance_file, task.test_checklist_file, task.output_json,
            ]),
        })
    return payloads


def _validate_task_fact_sources(paths: list[str]) -> dict[str, Any]:
    """LLM: verify whether task-directory authority files still exist."""

    missing = [p for p in paths if p and not Path(p).exists()]
    return {"ok": not missing, "missing_paths": missing}


def collect_gateway_payloads(local_hits: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:

    payloads: list[dict[str, Any]] = []
    for hit in local_hits:
        source_type = str(hit.get("source_type", "") or "")
        source_id = str(hit.get("source_id", "") or "")
        metadata = hit.get("metadata", {}) if isinstance(hit.get("metadata"), dict) else {}
        request_id = str(metadata.get("request_id") or source_id or "").strip()
        if source_type != "gateway_request" and not request_id.startswith("gwreq-"):
            continue
        request_path = _gateway_terminal_request_path(str(metadata.get("request_path", "") or "").strip())
        response_path = str(metadata.get("response_path", "") or "").strip()
        content_path = str(hit.get("content_path", "") or "").strip()
        recommended_paths = _dedupe_strings([request_path, response_path, content_path])
        payloads.append({
            "request_id": request_id,
            "source_id": source_id,
            "status": str(metadata.get("status", "") or ""),
            "ok": bool(metadata.get("ok", False)),
            "request_path": request_path,
            "response_path": response_path,
            "content_path": content_path,
            "recommended_read_paths": recommended_paths,
        })
        if len(payloads) >= max(limit, 0):
            break
    return payloads
