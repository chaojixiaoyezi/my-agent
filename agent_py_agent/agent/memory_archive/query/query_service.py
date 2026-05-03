from __future__ import annotations

"""LLM: query execution and filter application for memory archive.

新手说明:
这个文件放的是查询执行逻辑——收集归档记录、应用过滤器、管理分页。
它依赖 archive_io 读写文件，依赖 filter_policy 做过滤判断。
"""

from pathlib import Path
from typing import Any

from .archive_helpers import _append_run_id, _dedupe_strings
from .archive_io import _archive_files, _gateway_terminal_request_path, _read_archive_file
from .filter_policy import evaluate_filters
from .query_models import ArchiveQueryRequest, ArchiveQueryResponse, paginate_records


def execute_archive_query(
    root: Path,
    request: ArchiveQueryRequest,
) -> ArchiveQueryResponse:
    """LLM: execute a complete archive query and return paginated response.

    新手说明:
    收集归档记录，应用过滤条件，然后分页返回。

    参数说明:
    `root` 是工作区根目录；`request` 是查询请求参数对象。

    返回说明:
    返回分页的查询响应对象。
    """

    records = collect_raw_archive_records(root, request.layer, request.date_key, request.limit, request.level)
    filtered = apply_filters(
        records,
        query=request.query,
        filters=request.filters,
        since=request.since,
        until=request.until,
        level=request.level,
    )
    return paginate_records(
        filtered,
        page=request.page if hasattr(request, "page") else 1,
        page_size=request.page_size if hasattr(request, "page_size") else 100,
    )


def collect_raw_archive_records(
    root: Path,
    layer: str,
    date_key: str | None,
    limit: int,
    level: int | None = None,
) -> list[dict[str, Any]]:
    """LLM: load and normalize archive JSONL records from selected layers.

    新手说明:
    从选定的层（raw/hook/all）加载归档记录，统一格式后按时间倒序排列。

    参数说明:
    `root` 是工作区根目录；`layer` 是 raw、hook 或 all；`date_key` 是可选日期；
    `limit` 是返回上限，0 表示不截断；`level` 是可选的归档等级过滤。

    返回说明:
    返回按时间倒序排列的标准化归档记录列表。
    """

    records: list[dict[str, Any]] = []
    for current_layer, path in _archive_files(root, layer=layer, date_key=date_key):
        records.extend(_read_archive_file(current_layer, path))
    if level is not None:
        records = [r for r in records if int(r.get("archive_level", -1)) == int(level)]
    records.sort(key=lambda item: (item["created_at_sort"], item["file_path"], item["line_no"]), reverse=True)
    return records[:limit] if limit > 0 else records


def apply_filters(
    records: list[dict[str, Any]],
    *,
    query: str,
    filters: dict[str, str],
    since: str | None,
    until: str | None,
    level: int | None = None,
) -> list[dict[str, Any]]:
    """LLM: apply all filter predicates to a record list.

    新手说明:
    对记录列表应用所有过滤器，返回匹配通过的记录。

    参数说明:
    与 filter_archive_records 参数相同。

    返回说明:
    返回过滤后的记录列表，顺序沿用输入顺序。
    """

    return [
        record
        for record in records
        if evaluate_filters(
            record,
            query=query,
            filters=filters,
            since=since,
            until=until,
            level=level,
        )
    ]


def collect_task_payloads(agent, task_ids: list[str], *, limit: int) -> list[dict[str, Any]]:
    """LLM: load task fact-source paths for candidate subagent IDs.

    新手说明:
    这里读的是任务目录事实源。archive 只能提示"可能相关"，
    真正判断完成没完成，要回到 STATUS、WORK_LOG、HANDOFF、TESTS 这些文件。

    参数说明:
    `agent` 是当前 agent 对象；`task_ids` 是候选 subagent run id；`limit` 控制最多读取几个任务。

    返回说明:
    返回任务事实源摘要列表；任务不存在时返回带 `exists=False` 的记录。
    """

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
    """LLM: derive gateway request fact-source paths from LocalStore hits.

    新手说明:
    gateway 的最终事实源不是 LocalStore 摘要，而是请求/响应 JSON 文件。
    LocalStore 命中负责帮我们找到 request_id，这里再把 metadata 里的
    request_path、response_path、content_path 收成一张清单。

    参数说明:
    `local_hits` 是命中字典列表；`limit` 控制最多返回多少条。

    返回说明:
    返回 gateway fact-source 摘要列表；没有 gateway 命中时返回空列表。
    """

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
