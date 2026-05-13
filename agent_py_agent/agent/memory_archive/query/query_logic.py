# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

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
from .resume_guidance import ResumeGuidanceRequest, build_resume_guidance
from .task_sources import task_recovery_read_paths

# LLM: Resume guidance stays re-exported from query_logic for legacy tests while implementation lives in resume_guidance.py.
__all__ = [
    "CollectArchiveRecordsParams",
    "FilterArchiveRecordsParams",
    "ResumeGuidanceRequest",
    "archive_filters_from_args",
    "build_resume_guidance",
    "collect_archive_records",
    "collect_resume_task_ids",
    "filter_archive_records",
    "local_hit_payload",
    "resume_local_query",
    "strip_sort_keys",
]


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _ArchiveFilterContext 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _ArchiveFilterContext 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _ArchiveFilterContext:
    query_text: str
    filters: dict[str, str]
    since_ts: float | None
    until_ts: float | None
    level: int | None


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 CollectArchiveRecordsParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 CollectArchiveRecordsParams 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class CollectArchiveRecordsParams:
    # LLM: archive query fields stay bundled so resume/compact callers share one shape.
    layer: str
    date_key: str | None = None
    limit: int = 0
    level: int | None = None
    file_limit: int = 30


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 FilterArchiveRecordsParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 FilterArchiveRecordsParams 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class FilterArchiveRecordsParams:
    query: str = ""
    filters: dict[str, str] | None = None
    since: str | None = None
    until: str | None = None
    level: int | None = None


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 collect_archive_records 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 collect archive records 的候选结果，并按参数完成筛选、排序或数量限制。
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

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 archive_filters_from_args 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 archive filters from args 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
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

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 filter_archive_records 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 filter archive records 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
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
    query = str(values.query)
    filters = dict(values.filters or {})
    query_text = query.strip().lower()
    context = _ArchiveFilterContext(
        query_text=query_text,
        filters=filters,
        since_ts=_created_at_sort(values.since or "", fallback=0.0) if values.since else None,
        until_ts=_until_timestamp(values.until),
        level=values.level,
    )
    return [
        record
        for record in records
        if _archive_record_matches(record, context)
    ]

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 resume_local_query 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 resume local query 的候选结果，并按参数完成筛选、排序或数量限制。
def resume_local_query(args, archive_matches: list[dict[str, Any]]) -> str:
    for value in (args.query, args.run_id, args.request_id, args.session_id, args.task_id):
        text = str(value or "").strip()
        if text:
            return text
    for record in archive_matches:
        if text := _first_record_id(record):
            return text
    return ""

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 local_hit_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 local hit payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 collect_resume_task_ids 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 collect resume task ids 的候选结果，并按参数完成筛选、排序或数量限制。
def collect_resume_task_ids(args, archive_matches: list[dict[str, Any]], local_hits: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for value in (args.run_id, args.task_id):
        _append_run_id(ids, value)
    for record in archive_matches:
        _append_archive_task_ids(ids, record)
    for hit in local_hits:
        _append_local_hit_task_ids(ids, hit)
    return ids

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 collect_task_payloads 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 collect task payloads 的候选结果，并按参数完成筛选、排序或数量限制。
def collect_task_payloads(agent, task_ids: list[str], *, limit: int) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for run_id in task_ids[:limit]:
        payloads.append(_task_payload(agent, run_id))
    return payloads

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 collect_gateway_payloads 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 collect gateway payloads 的候选结果，并按参数完成筛选、排序或数量限制。
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

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 strip_sort_keys 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 strip sort keys 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
def strip_sort_keys(payload: Any) -> Any:
    if isinstance(payload, list):
        return [strip_sort_keys(item) for item in payload]
    if isinstance(payload, dict):
        return {key: strip_sort_keys(value) for key, value in payload.items() if key != "created_at_sort"}
    return payload

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _until_timestamp 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 until timestamp 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _until_timestamp(until: str | None) -> float | None:
    if not until:
        return None
    timestamp = _created_at_sort(until, fallback=0.0)
    if _is_date_only(until):
        timestamp += 86399.999999
    return timestamp

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _archive_record_matches 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 archive record matches 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
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

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _matches_filters 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 matches filters 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def _matches_filters(record: dict[str, Any], filters: dict[str, str]) -> bool:
    return all(str(record.get(field, "")) == value for field, value in filters.items())

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _matches_level 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 matches level 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def _matches_level(record: dict[str, Any], level: int | None) -> bool:
    return level is None or int(record.get("archive_level", -1)) == int(level)

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _first_record_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 first record id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _first_record_id(record: dict[str, Any]) -> str:
    for field in ("run_id", "task_id", "request_id", "session_id"):
        text = str(record.get(field, "") or "").strip()
        if text:
            return text
    return ""

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _append_archive_task_ids 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append archive task ids 相关记录，集中处理目标路径、格式化和状态更新。
def _append_archive_task_ids(ids: list[str], record: dict[str, Any]) -> None:
    _append_run_id(ids, record.get("run_id"))
    _append_run_id(ids, record.get("task_id"))
    for ref in record.get("task_refs", []) or []:
        _append_run_id(ids, ref)

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _append_local_hit_task_ids 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append local hit task ids 相关记录，集中处理目标路径、格式化和状态更新。
def _append_local_hit_task_ids(ids: list[str], hit: dict[str, Any]) -> None:
    if str(hit.get("source_type", "")).startswith("subagent"):
        _append_run_id(ids, hit.get("source_id"))
    metadata = hit.get("metadata", {}) if isinstance(hit.get("metadata"), dict) else {}
    _append_run_id(ids, metadata.get("run_id"))
    _append_run_id(ids, metadata.get("task_id"))

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _task_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 task payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _task_read_paths 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 task read paths 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
def _task_read_paths(task) -> list[str]:
    # LLM: keep legacy query_logic callers aligned with query_service task sources.
    return task_recovery_read_paths(task)

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _validate_task_fact_sources 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 validate task fact sources 的输入、状态或路径，提前暴露无效数据和越界条件。
def _validate_task_fact_sources(paths: list[str]) -> dict[str, Any]:
    missing = [path for path in paths if path and not Path(path).exists()]
    return {"ok": not missing, "missing_paths": missing}

# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _gateway_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 gateway payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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
