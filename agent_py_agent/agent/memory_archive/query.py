from __future__ import annotations

"""LLM: query helpers for memory raw archive, hook snapshots, and resume evidence.

新手说明:
这个文件只做“找线索”和“整理恢复依据”。
命令怎么打印放在 `memory_archive_commands.py`，这样查询逻辑可以单独测试，也不会把 CLI 文件堆大。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import raw_event_path_for, snapshot_path_for

ARCHIVE_SEARCH_FILE_LIMIT = 30


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
    这里统一成同一种“可搜索卡片”，后面的 list/search/resume 就不用关心原始格式差异。

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
    archive 只能提示“可能相关”，真正判断完成没完成，要回到 STATUS、WORK_LOG、HANDOFF、TESTS 这些文件。

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
        payloads.append(
            {
                "run_id": task.id,
                "exists": True,
                "status": task.status,
                "verification_status": task.verification_status,
                "goal": task.goal,
                "updated_at": task.updated_at,
                "task_dir": task.task_dir,
                "recommended_read_paths": [
                    task.status_file,
                    task.work_log_file,
                    task.handoff_file,
                    task.acceptance_file,
                    task.test_checklist_file,
                    task.output_json,
                ],
                "authority_validation": _validate_task_fact_sources(
                    [
                        task.status_file,
                        task.work_log_file,
                        task.handoff_file,
                        task.acceptance_file,
                        task.test_checklist_file,
                        task.output_json,
                    ]
                ),
            }
        )
    return payloads


def _validate_task_fact_sources(paths: list[str]) -> dict[str, Any]:
    """LLM: verify whether task-directory authority files still exist for resume validation.

    新手说明:
    恢复流程不能只看摘要，所以这里顺手校验权威文件路径，防止 snapshot 指向已经失效的 task 现场。
    """

    missing = [path for path in paths if path and not Path(path).exists()]
    return {
        "ok": not missing,
        "missing_paths": missing,
    }


def collect_gateway_payloads(local_hits: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """LLM: derive gateway request fact-source paths from LocalStore hits.

    新手说明:
    gateway 的最终事实源不是 LocalStore 摘要，而是请求/响应 JSON 文件。
    LocalStore 命中负责帮我们找到 request_id，这里再把 metadata 里的 request_path、response_path、
    content_path 收成一张“该读哪些文件”的清单。

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
        payloads.append(
            {
                "request_id": request_id,
                "source_id": source_id,
                "status": str(metadata.get("status", "") or ""),
                "ok": bool(metadata.get("ok", False)),
                "request_path": request_path,
                "response_path": response_path,
                "content_path": content_path,
                "recommended_read_paths": recommended_paths,
            }
        )
        if len(payloads) >= max(limit, 0):
            break
    return payloads


def _gateway_terminal_request_path(request_path: str) -> str:
    """LLM: prefer completed gateway request archive paths over transient processing paths.

    新手说明:
    真实 gateway 会先把请求放在 `requests/processing/`，处理完成后再移动到
    `requests/done/` 或 `requests/failed/`。LocalStore 可能记录的是处理中的临时路径，
    恢复时应该优先指向最终还存在的归档文件，避免第二天按提示去读一个已经被移动走的路径。
    参数说明:
    `request_path` 是 LocalStore metadata 里记录的请求 JSON 路径，可能为空、可能是 processing 路径。
    返回说明:
    返回最适合恢复读取的路径；如果找不到更好的终态文件，就保持原值。
    """

    if not request_path:
        return ""
    path = Path(request_path)
    parts = list(path.parts)
    try:
        requests_index = parts.index("requests")
        state_index = requests_index + 1
    except ValueError:
        if path.exists():
            return request_path
        return request_path
    if state_index >= len(parts) or parts[state_index] != "processing":
        if path.exists():
            return request_path
        return request_path
    candidates: list[Path] = []
    for terminal_state in ("done", "failed"):
        updated_parts = parts[:]
        updated_parts[state_index] = terminal_state
        candidates.append(Path(*updated_parts))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return request_path


def build_resume_guidance(
    archive_matches: list[dict[str, Any]],
    local_hits: list[dict[str, Any]],
    task_payloads: list[dict[str, Any]],
    gateway_payloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """LLM: summarize recovery clues into next reads and safe next actions.

    新手说明:
    恢复命令不能直接替用户下结论。
    它应该告诉你“找到了哪些线索、先读哪些权威文件、下一步怎么核对”。

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
        next_actions.append(
            "发现部分 task 权威文件缺失，恢复前必须先修复事实源：" + ", ".join(invalid_authority[:5])
        )
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


def _archive_files(root: Path, *, layer: str, date_key: str | None) -> list[tuple[str, Path]]:
    """LLM: return existing archive files for raw/hook layers in newest-first order.

    新手说明:
    如果指定日期，就只看那天的文件；没指定日期，就看最近若干个 JSONL 文件。
    这样不会为了一个 list/search 命令把多年归档一次性翻完。

    参数说明:
    `root` 是工作区根目录；`layer` 是 `raw`、`hook` 或 `all`；`date_key` 是可选日期。

    返回说明:
    返回 `(layer, path)` 元组列表，按最近优先排列。
    """

    layers = ["raw", "hook"] if layer == "all" else [layer]
    files: list[tuple[str, Path]] = []
    for current_layer in layers:
        directory = _archive_dir(root, current_layer)
        if date_key:
            candidate = directory / f"{date_key}.jsonl"
            if candidate.exists():
                files.append((current_layer, candidate))
            continue
        if directory.exists():
            layer_files = sorted(
                [path for path in directory.glob("*.jsonl") if path.is_file()],
                key=lambda path: (path.stat().st_mtime, path.name),
                reverse=True,
            )[:ARCHIVE_SEARCH_FILE_LIMIT]
            files.extend((current_layer, path) for path in layer_files)
    return files


def _archive_dir(root: Path, layer: str) -> Path:
    """LLM: resolve the directory for one archive layer through public path helpers.

    新手说明:
    目录规则不要散落在 CLI 里。
    hook 用 `snapshot_path_for`，raw 用 `raw_event_path_for`，以后路径变了这里也能跟着变。

    参数说明:
    `root` 是工作区根目录；`layer` 是 `hook` 或 `raw`。

    返回说明:
    返回对应归档目录路径。
    """

    return snapshot_path_for(root).parent if layer == "hook" else raw_event_path_for(root).parent


def _read_archive_file(layer: str, path: Path) -> list[dict[str, Any]]:
    """LLM: parse one archive JSONL file and skip malformed lines without crashing.

    新手说明:
    归档是排障兜底层。
    即使里面有一行坏 JSON，命令也应该继续读其他行，并把坏行标出来，而不是直接中断。

    参数说明:
    `layer` 是当前层名；`path` 是 JSONL 文件路径。

    返回说明:
    返回标准化记录列表；坏行会变成 `archive_error` 记录。
    """

    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [_archive_error_record(layer, path, line_no=0, message=f"{type(exc).__name__}: {exc}")]
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            records.append(_archive_error_record(layer, path, line_no=line_no, message=str(exc)))
            continue
        if not isinstance(payload, dict):
            records.append(_archive_error_record(layer, path, line_no=line_no, message="record is not a JSON object"))
            continue
        records.append(_normalize_archive_record(layer, path, line_no, payload))
    return records


def _normalize_archive_record(layer: str, path: Path, line_no: int, payload: dict[str, Any]) -> dict[str, Any]:
    """LLM: map raw event or hook snapshot payloads to a shared search/display shape.

    新手说明:
    raw 事件有 event_id、speaker、tool_name；hook 快照有 snapshot_id、user_intents、next_actions。
    统一后，搜索命令就能按同一套字段工作。

    参数说明:
    `layer` 是 raw/hook；`path` 是来源文件；`line_no` 是行号；`payload` 是原始 JSON 对象。

    返回说明:
    返回标准化归档记录。
    """

    derived = _derived_archive_fields(payload)
    created_at = str(payload.get("created_at", "") or "")
    record_id = str(payload.get("event_id") or payload.get("snapshot_id") or f"{path.name}:{line_no}")
    return {
        "layer": layer,
        "kind": "hook_snapshot" if layer == "hook" else "raw_event",
        "id": record_id,
        "session_id": str(payload.get("session_id", "") or ""),
        "request_id": str(payload.get("request_id") or derived.get("request_id") or ""),
        "run_id": str(payload.get("run_id") or derived.get("run_id") or ""),
        "task_id": str(payload.get("task_id") or derived.get("task_id") or ""),
        "speaker": str(payload.get("speaker", "") or ""),
        "target": str(payload.get("target", "") or ""),
        "action": str(payload.get("action", "snapshot" if layer == "hook" else "") or ""),
        "status": str(payload.get("status") or derived.get("status") or ""),
        "error_code": str(payload.get("error_code") or derived.get("error_code") or ""),
        "is_dispatch": bool(payload.get("is_dispatch", False)),
        "tool_name": str(payload.get("tool_name", "") or ""),
        "tool_success": payload.get("tool_success"),
        "source": str(payload.get("source") or derived.get("source") or ""),
        "archive_level": _archive_level_value(payload.get("archive_level", 3)),
        "created_at": created_at,
        "created_at_sort": _created_at_sort(created_at, fallback=path.stat().st_mtime),
        "content_preview": _archive_preview(payload),
        "content_path": str(payload.get("content_path", "") or ""),
        "content_hash": str(payload.get("content_hash", "") or ""),
        "task_refs": [str(item) for item in _list_value(payload.get("task_refs"))],
        "next_actions": [str(item) for item in _list_value(payload.get("next_actions"))],
        "file_path": str(path),
        "line_no": line_no,
        "payload": payload,
    }


def _archive_level_value(value: Any) -> int:
    """LLM: keep archive level stable even when the stored value is numeric zero.

    新手说明:
    `0` 是合法 archive level，不能因为布尔短路被误回退到 3。
    """

    try:
        return int(3 if value is None else value)
    except (TypeError, ValueError):
        return 3


def _derived_archive_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """LLM: derive request/run/task/status/source fields from hook internals.

    新手说明:
    hook snapshot 没有顶层 request_id/run_id。
    这些字段通常藏在 `turn_range` 或 `dispatch_events` 里，恢复搜索时要提出来，否则 `--run-id` 会漏掉 hook。

    参数说明:
    `payload` 是 hook/raw 原始记录。

    返回说明:
    返回推导出的 request/run/task/status/source 字段字典。
    """

    fields: dict[str, Any] = {}
    turn_range = payload.get("turn_range")
    if isinstance(turn_range, dict):
        for key in ("request_id", "run_id", "task_id", "source", "status", "error_code"):
            if turn_range.get(key):
                fields[key] = turn_range.get(key)
    dispatch_events = payload.get("dispatch_events")
    if isinstance(dispatch_events, list):
        for event in dispatch_events:
            if not isinstance(event, dict):
                continue
            for key in ("request_id", "run_id", "task_id", "source", "status", "error_code"):
                if event.get(key) and not fields.get(key):
                    fields[key] = event.get(key)
    return fields


def _archive_preview(payload: dict[str, Any]) -> str:
    """LLM: derive a compact human-readable preview from raw or hook payloads.

    新手说明:
    不同类型的归档正文位置不一样。
    这里优先拿 content_preview；如果没有，就从意图、动作、决策、下一步里拼一个短摘要。

    参数说明:
    `payload` 是原始归档记录。

    返回说明:
    返回最多 500 字符的预览文本。
    """

    preview = str(payload.get("content_preview", "") or "").strip()
    if preview:
        return preview
    parts: list[str] = []
    for key in ("user_intents", "assistant_actions", "decisions", "open_questions", "next_actions", "task_refs"):
        value = payload.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value if str(item).strip())
    return "；".join(parts)[:500]


def _archive_error_record(layer: str, path: Path, *, line_no: int, message: str) -> dict[str, Any]:
    """LLM: represent malformed archive lines as searchable diagnostic records.

    新手说明:
    坏行也算一种线索。
    用户至少应该知道哪个文件第几行坏了，而不是看到命令静悄悄漏掉内容。

    参数说明:
    `layer` 是 raw/hook；`path` 是出错文件；`line_no` 是行号；`message` 是错误说明。

    返回说明:
    返回一条标准化错误记录。
    """

    return {
        "layer": layer,
        "kind": "archive_error",
        "id": f"{path.name}:{line_no}:error",
        "session_id": "",
        "request_id": "",
        "run_id": "",
        "task_id": "",
        "speaker": "",
        "target": "",
        "action": "parse_error",
        "status": "failed",
        "error_code": "archive_json_decode_error",
        "is_dispatch": False,
        "tool_name": "",
        "tool_success": None,
        "source": "",
        "created_at": "",
        "created_at_sort": path.stat().st_mtime if path.exists() else 0.0,
        "content_preview": message,
        "content_path": "",
        "content_hash": "",
        "task_refs": [],
        "next_actions": [],
        "file_path": str(path),
        "line_no": line_no,
        "payload": {"error": message},
    }


def _archive_search_text(record: dict[str, Any]) -> str:
    """LLM: build the searchable text blob for one normalized archive record.

    新手说明:
    关键词不只搜正文预览，也搜 ID、工具名、状态、任务引用和原始 payload。
    这样用户记得某个 request_id 时也能找回来。

    参数说明:
    `record` 是标准化归档记录。

    返回说明:
    返回用于小写关键词匹配的大文本。
    """

    parts = [
        str(record.get("id", "")),
        str(record.get("session_id", "")),
        str(record.get("request_id", "")),
        str(record.get("run_id", "")),
        str(record.get("task_id", "")),
        str(record.get("speaker", "")),
        str(record.get("target", "")),
        str(record.get("action", "")),
        str(record.get("status", "")),
        str(record.get("tool_name", "")),
        str(record.get("source", "")),
        str(record.get("content_preview", "")),
        " ".join(record.get("task_refs", []) or []),
        " ".join(record.get("next_actions", []) or []),
        json.dumps(record.get("payload", {}), ensure_ascii=False, sort_keys=True),
    ]
    return "\n".join(parts).lower()


def _append_run_id(items: list[str], value: object) -> None:
    """LLM: extract and append one subagent run ID from a loose text value.

    新手说明:
    有些地方存的是完整句子，比如“请看 subagent-xxx”。
    这个函数把里面真正的 `subagent-*` ID 挖出来，避免恢复时找不到任务目录。

    参数说明:
    `items` 是要追加的 ID 列表；`value` 是可能包含 subagent ID 的任意值。

    返回说明:
    不返回值；可能原地追加一个 ID。
    """

    text = str(value or "").strip()
    if not text or "subagent-" not in text:
        return
    run_id = text[text.find("subagent-") :].split()[0].strip("`'\",)")
    if run_id and run_id not in items:
        items.append(run_id)


def _dedupe_strings(values: list[str]) -> list[str]:
    """LLM: keep non-empty strings once while preserving order.

    新手说明:
    gateway 的 request_path、response_path、content_path 可能有空值，也可能重复。
    这里收成干净列表，给 Recovery Brief 和 CLI 输出直接使用。

    参数说明:
    `values` 是候选路径字符串列表。

    返回说明:
    返回去空、去重后的字符串列表。
    """

    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def _created_at_sort(value: str, *, fallback: float) -> float:
    """LLM: convert an ISO-like timestamp to a sortable epoch value.

    新手说明:
    归档记录要按时间倒序显示。
    如果时间字符串坏了，就用文件修改时间这类 fallback，保证命令还能继续跑。

    参数说明:
    `value` 是 ISO-like 时间字符串；`fallback` 是解析失败时使用的时间戳。

    返回说明:
    返回 epoch 秒数。
    """

    text = str(value or "").strip()
    if not text:
        return fallback
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text[:10])
        except ValueError:
            return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _is_date_only(value: str) -> bool:
    """LLM: detect `YYYY-MM-DD` filters that should mean the whole day.

    新手说明:
    用户写 `--until 2026-04-30` 时，通常想包含 4 月 30 日全天，而不是只到当天 00:00。
    这个 helper 只识别最朴素的日期格式，让 `filter_archive_records()` 可以把 until 推到当天末尾。

    参数说明:
    `value` 是用户传入的 since/until 字符串。

    返回说明:
    形如 `YYYY-MM-DD` 时返回 True，否则 False。
    """

    text = str(value or "").strip()
    if len(text) != 10:
        return False
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return text[4] == "-" and text[7] == "-"


def _list_value(value: object) -> list[object]:
    """LLM: normalize a scalar-or-list payload field into a list.

    新手说明:
    JSON 里有些字段可能是单个字符串，也可能已经是列表。
    这里统一成列表，后面搜索和展示就不用到处判断类型。

    参数说明:
    `value` 是待归一化字段。

    返回说明:
    返回列表；空值返回空列表。
    """

    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]
