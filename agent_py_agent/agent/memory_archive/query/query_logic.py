from __future__ import annotations

"""LLM: public query functions for memory archive search and resume evidence.

新手说明:
这个文件放的是"对外"的查询函数——收集归档记录、构造过滤器、过滤记录、
恢复本地查询、拼装 payload、生成恢复指导、剥离排序键。
命令层和测试只应该 import 这里，不应该直接碰 archive_io 或 archive_helpers。
"""

import json
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


def collect_archive_records(
    root: Path,
    *,
    layer: str,
    date_key: str | None,
    limit: int,
    level: int | None = None,
) -> list[dict[str, Any]]:
    """LLM: load and normalize archive JSONL records from selected layers.

    新手说明:
    raw 和 hook 的字段不一样。
    这里统一成同一种"可搜索卡片"，后面的 list/search/resume 就不用关心原始格式差异。

    参数说明:
    `root` 是工作区根目录；`layer` 是 `raw`、`hook` 或 `all`；
    `date_key` 是指定日期字符串，例如 `2026-04-30`；`limit` 是返回上限，0 表示不截断。

    返回说明:
    返回按时间倒序排列的标准化归档记录列表。
    """

    records: list[dict[str, Any]] = []
    for current_layer, path in _archive_files(root, layer=layer, date_key=date_key):
        records.extend(_read_archive_file(current_layer, path))
    if level is not None:
        records = [record for record in records if int(record.get("archive_level", -1)) == int(level)]
    records.sort(key=lambda item: (item["created_at_sort"], item["file_path"], item["line_no"]), reverse=True)
    return records[:limit] if limit > 0 else records


def archive_filters_from_args(args) -> dict[str, str]:
    """LLM: extract exact-match archive filters from argparse args.

    新手说明:
    只把用户真的传了的字段放进过滤器。
    空字符串不参与过滤，避免用户没填某个字段时误把所有记录过滤掉。

    参数说明:
    `args` 是 argparse 参数对象，可能带 session_id、request_id、run_id 等字段。

    返回说明:
    返回需要精确匹配的字段和值。
    """

    fields = [
        "session_id", "request_id", "run_id", "task_id",
        "speaker", "target", "action", "status", "tool_name", "source",
    ]
    return {
        field: str(getattr(args, field, "") or "").strip()
        for field in fields
        if str(getattr(args, field, "") or "").strip()
    }


def filter_archive_records(
    records: list[dict[str, Any]],
    *,
    query: str,
    filters: dict[str, str],
    since: str | None,
    until: str | None,
    level: int | None = None,
) -> list[dict[str, Any]]:
    """LLM: apply keyword, exact-field, and time-window filters to normalized records.

    新手说明:
    先按字段精准过滤，再按时间窗口过滤，最后按关键词在关键字段里搜。
    这样 `--run-id xxx` 这类精确恢复不会被普通关键词噪声干扰。

    参数说明:
    `records` 是标准化归档记录；`query` 是关键词；`filters` 是精确字段过滤；
    `since` 和 `until` 是可选时间边界。

    返回说明:
    返回匹配到的记录列表，顺序沿用输入顺序。
    """

    query_text = query.strip().lower()
    since_ts = _created_at_sort(since or "", fallback=0.0) if since else None
    until_ts = _created_at_sort(until or "", fallback=0.0) if until else None
    if until_ts is not None and _is_date_only(until or ""):
        until_ts += 86399.999999
    matches: list[dict[str, Any]] = []
    for record in records:
        if any(str(record.get(field, "")) != value for field, value in filters.items()):
            continue
        if level is not None and int(record.get("archive_level", -1)) != int(level):
            continue
        created_at = float(record.get("created_at_sort", 0.0) or 0.0)
        if since_ts is not None and created_at < since_ts:
            continue
        if until_ts is not None and created_at > until_ts:
            continue
        if query_text and query_text not in _archive_search_text(record):
            continue
        matches.append(record)
    return matches


def resume_local_query(args, archive_matches: list[dict[str, Any]]) -> str:
    """LLM: choose the LocalStore query used by memory-resume.

    新手说明:
    用户明确给关键词就搜关键词；没给关键词但给了 run_id/request_id，就搜这个 ID。
    如果 archive 已经命中了 run_id，也用 run_id 去 LocalStore 里找权威任务记录。

    参数说明:
    `args` 是 CLI 或自动恢复构造的参数对象；`archive_matches` 是已命中的归档线索。

    返回说明:
    返回 LocalStore 搜索关键词；没有可用线索时返回空字符串。
    """

    for value in (args.query, args.run_id, args.request_id, args.session_id, args.task_id):
        text = str(value or "").strip()
        if text:
            return text
    for record in archive_matches:
        for field in ("run_id", "task_id", "request_id", "session_id"):
            text = str(record.get(field, "") or "").strip()
            if text:
                return text
    return ""


def local_hit_payload(hit) -> dict[str, Any]:
    """LLM: serialize a LocalStore search hit for resume output.

    新手说明:
    恢复命令只需要来源、标题、正文预览、metadata 和路径。
    完整正文仍然留在 LocalStore 文件里，避免命令输出过大。

    参数说明:
    `hit` 是 LocalStore 返回的搜索命中对象。

    返回说明:
    返回 JSON 友好的命中字典。
    """

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
    """LLM: derive candidate subagent run IDs from filters, archive records, and LocalStore hits.

    新手说明:
    恢复任务时最重要的是找到任务目录。
    这里尽量从 run_id、task_id、LocalStore 的 subagent source_id 里提取 `subagent-*`。

    参数说明:
    `args` 是恢复参数；`archive_matches` 是归档线索；`local_hits` 是 LocalStore 命中字典。

    返回说明:
    返回去重后的候选 subagent run id 列表。
    """

    ids: list[str] = []
    for value in (args.run_id, args.task_id):
        _append_run_id(ids, value)
    for record in archive_matches:
        _append_run_id(ids, record.get("run_id"))
        _append_run_id(ids, record.get("task_id"))
        for ref in record.get("task_refs", []) or []:
            _append_run_id(ids, ref)
    for hit in local_hits:
        if str(hit.get("source_type", "")).startswith("subagent"):
            _append_run_id(ids, hit.get("source_id"))
        metadata = hit.get("metadata", {}) if isinstance(hit.get("metadata"), dict) else {}
        _append_run_id(ids, metadata.get("run_id"))
        _append_run_id(ids, metadata.get("task_id"))
    return ids


def collect_task_payloads(agent, task_ids: list[str], *, limit: int) -> list[dict[str, Any]]:
    """LLM: load task fact-source paths for candidate subagent IDs.

    新手说明:
    这里读的是任务目录事实源。
    archive 只能提示"可能相关"，真正判断完成没完成，要回到 STATUS、WORK_LOG、HANDOFF、TESTS 这些文件。

    参数说明:
    `agent` 是当前 agent 对象；`task_ids` 是候选 subagent run id；`limit` 控制最多读取几个任务。

    返回说明:
    返回任务事实源摘要列表；任务不存在时返回带 `exists=False` 的记录。
    """

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
    """LLM: verify whether task-directory authority files still exist for resume validation.

    新手说明:
    恢复不能只信摘要，所以至少要确认关键 task 文件还在。
    """

    missing = [path for path in paths if path and not Path(path).exists()]
    return {"ok": not missing, "missing_paths": missing}


def collect_gateway_payloads(local_hits: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """LLM: derive gateway request fact-source paths from LocalStore hits.

    新手说明:
    gateway 的最终事实源不是 LocalStore 摘要，而是请求/响应 JSON 文件。
    LocalStore 命中负责帮我们找到 request_id，这里再把 metadata 里的 request_path、response_path、
    content_path 收成一张"该读哪些文件"的清单。

    参数说明:
    `local_hits` 是 `local_hit_payload()` 返回的命中字典列表；`limit` 控制最多返回多少条。

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


def build_resume_guidance(
    archive_matches: list[dict[str, Any]],
    local_hits: list[dict[str, Any]],
    task_payloads: list[dict[str, Any]],
    gateway_payloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """LLM: summarize recovery clues into next reads and safe next actions.

    新手说明:
    恢复命令不能直接替用户下结论。
    它应该告诉你"找到了哪些线索、先读哪些权威文件、下一步怎么核对"。

    参数说明:
    `archive_matches`、`local_hits`、`task_payloads` 分别是归档线索、LocalStore 线索和任务事实源。
    `gateway_payloads` 是可选的 gateway request/response 事实源摘要。

    返回说明:
    返回恢复指导字典，包含数量摘要、推荐阅读路径和下一步动作。
    """

    recommended_reads: list[str] = []
    for task in task_payloads:
        for path in task.get("recommended_read_paths", []) or []:
            if path and path not in recommended_reads:
                recommended_reads.append(path)
    for gateway in gateway_payloads or []:
        for path in gateway.get("recommended_read_paths", []) or []:
            if path and path not in recommended_reads:
                recommended_reads.append(path)
    for hit in local_hits:
        path = str(hit.get("content_path", "") or "")
        if path and path not in recommended_reads:
            recommended_reads.append(path)
    next_actions = [
        "先读取 task_fact_sources 里的 STATUS/WORK_LOG/HANDOFF/TESTS，再判断任务是否能继续。",
        "把 archive_matches 当恢复线索，不要把其中的历史摘要当最终事实。",
    ]
    invalid_authority = [
        task.get("run_id", "")
        for task in task_payloads
        if isinstance(task.get("authority_validation"), dict) and not task["authority_validation"].get("ok", False)
    ]
    if invalid_authority:
        next_actions.append("发现部分 task 权威文件缺失，恢复前必须先修复事实源：" + ", ".join(invalid_authority[:5]))
    if not task_payloads and not gateway_payloads:
        next_actions.append("如果没有任务目录，先用 memory-archive-search 缩小 request_id/run_id/session_id。")
    return {
        "archive_match_count": len(archive_matches),
        "local_match_count": len(local_hits),
        "task_fact_source_count": len(task_payloads),
        "gateway_fact_source_count": len(gateway_payloads or []),
        "recommended_read_paths": recommended_reads[:20],
        "next_actions": next_actions,
    }


def strip_sort_keys(payload: Any) -> Any:
    """LLM: remove internal sort keys from JSON output recursively.

    新手说明:
    `created_at_sort` 只是命令内部排序用的数字。
    输出给用户时去掉它，避免用户误以为这是正式业务字段。

    参数说明:
    `payload` 可以是 dict、list 或普通值。

    返回说明:
    返回递归移除 `created_at_sort` 后的新对象。
    """

    if isinstance(payload, list):
        return [strip_sort_keys(item) for item in payload]
    if isinstance(payload, dict):
        return {key: strip_sort_keys(value) for key, value in payload.items() if key != "created_at_sort"}
    return payload
