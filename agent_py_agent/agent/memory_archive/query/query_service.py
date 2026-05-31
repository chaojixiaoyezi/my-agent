# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""query execution and filter application for memory archive.

新手说明:
这个文件放的是查询执行逻辑——收集归档记录、应用过滤器、管理分页。
它依赖 archive_io 读写文件，依赖 filter_policy 做过滤判断。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...user_space.home_runtime_query import home_task_workspace_payload
from .archive_helpers import _append_run_id, _dedupe_strings
from .archive_io import _archive_files, _gateway_terminal_request_path, _read_archive_file
from .filter_policy import ArchiveFilterOptions, evaluate_filters
from .query_models import ArchiveQueryRequest, ArchiveQueryResponse, paginate_records
from .task_sources import task_recovery_read_paths


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 RawArchiveCollectOptions 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 RawArchiveCollectOptions 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class RawArchiveCollectOptions:
    layer: str
    date_key: str | None
    limit: int
    level: int | None = None


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 execute_archive_query 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 推进 execute archive query 对应的调度、执行或处理步骤，并返回可追踪的状态结果。
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


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 collect_raw_archive_records 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 collect raw archive records 的候选结果，并按参数完成筛选、排序或数量限制。
def collect_raw_archive_records(
    root: Path,
    options: RawArchiveCollectOptions,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for current_layer, path in _archive_files(root, layer=options.layer, date_key=options.date_key):
        records.extend(_read_archive_file(current_layer, path))
    if options.level is not None:
        records = [r for r in records if int(r.get("archive_level", -1)) == int(options.level)]
    records.sort(key=lambda item: (item["created_at_sort"], item["file_path"], item["line_no"]), reverse=True)
    return records[:options.limit] if options.limit > 0 else records


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 apply_filters 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 apply filters 在当前模块中的核心转换或协调步骤，衔接 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实。
def apply_filters(
    records: list[dict[str, Any]],
    options: ArchiveFilterOptions,
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if evaluate_filters(record, options=options)
    ]


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 collect_task_payloads 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 collect task payloads 的候选结果，并按参数完成筛选、排序或数量限制。
def collect_task_payloads(agent, task_ids: list[str], *, limit: int) -> list[dict[str, Any]]:

    import json

    payloads: list[dict[str, Any]] = []
    for run_id in task_ids[:limit]:
        try:
            task = agent.subagents.load(run_id)
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            payloads.append(_missing_or_home_task_payload(agent, run_id))
            continue
        # LLM: use one compact-first source list for CLI resume and runtime resume.
        paths = task_recovery_read_paths(task)
        payloads.append({
            "run_id": task.id, "exists": True, "status": task.status,
            "verification_status": task.verification_status, "goal": task.goal,
            "updated_at": task.updated_at, "task_dir": task.task_dir,
            "recommended_read_paths": paths,
            "authority_validation": _validate_task_fact_sources(paths),
        })
    return payloads


# LLM: _missing_or_home_task_payload upgrades resume from legacy-only subagents to home task workspace refs.
# 函数用途: 旧 subagent 工单不存在时，尝试从 ~/my-agent/tasks 找主代理任务事实源。
def _missing_or_home_task_payload(agent, run_id: str) -> dict[str, Any]:
    payload = home_task_workspace_payload(agent.home_paths, run_id)
    if payload is not None:
        return payload
    return {"run_id": run_id, "exists": False, "error": "task not found"}


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 _validate_task_fact_sources 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 validate task fact sources 的输入、状态或路径，提前暴露无效数据和越界条件。
def _validate_task_fact_sources(paths: list[str]) -> dict[str, Any]:
    """verify whether task-directory authority files still exist."""

    missing = [p for p in paths if p and not Path(p).exists()]
    return {"ok": not missing, "missing_paths": missing}


# LLM: 归档查询从 archive JSON/JSONL 与 workspace 文件读取可恢复事实；修改 collect_gateway_payloads 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 collect gateway payloads 的候选结果，并按参数完成筛选、排序或数量限制。
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
