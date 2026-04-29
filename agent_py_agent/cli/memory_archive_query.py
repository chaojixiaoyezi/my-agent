from __future__ import annotations

"""LLM: query helpers for memory raw archive, hook snapshots, and resume evidence.

给人看的解释：
这个文件只做“找线索”和“整理恢复依据”。
命令怎么打印放在 `memory_archive_commands.py`，这样查询逻辑可以单独测试，也不会把 CLI 文件堆大。
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agent.memory_archive import raw_event_path_for, snapshot_path_for


ARCHIVE_SEARCH_FILE_LIMIT = 30


def collect_archive_records(
    root: Path,
    *,
    layer: str,
    date_key: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """LLM: load and normalize archive JSONL records from selected layers.

    大白话：raw 和 hook 的字段不一样。
    这里统一成同一种“可搜索卡片”，后面的 list/search/resume 就不用关心原始格式差异。
    """

    records: list[dict[str, Any]] = []
    for current_layer, path in _archive_files(root, layer=layer, date_key=date_key):
        records.extend(_read_archive_file(current_layer, path))
    records.sort(key=lambda item: (item["created_at_sort"], item["file_path"], item["line_no"]), reverse=True)
    return records[:limit] if limit > 0 else records


def archive_filters_from_args(args) -> dict[str, str]:
    """LLM: extract exact-match archive filters from argparse args.

    大白话：只把用户真的传了的字段放进过滤器。
    空字符串不参与过滤，避免用户没填某个字段时误把所有记录过滤掉。
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
) -> list[dict[str, Any]]:
    """LLM: apply keyword, exact-field, and time-window filters to normalized records.

    大白话：先按字段精准过滤，再按时间窗口过滤，最后按关键词在关键字段里搜。
    这样 `--run-id xxx` 这类精确恢复不会被普通关键词噪声干扰。
    """

    query_text = query.strip().lower()
    since_ts = _created_at_sort(since or "", fallback=0.0) if since else None
    until_ts = _created_at_sort(until or "", fallback=0.0) if until else None
    matches: list[dict[str, Any]] = []
    for record in records:
        if any(str(record.get(field, "")) != value for field, value in filters.items()):
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

    大白话：用户明确给关键词就搜关键词；没给关键词但给了 run_id/request_id，就搜这个 ID。
    如果 archive 已经命中了 run_id，也用 run_id 去 LocalStore 里找权威任务记录。
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

    大白话：恢复命令只需要来源、标题、正文预览、metadata 和路径。
    完整正文仍然留在 LocalStore 文件里，避免命令输出过大。
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

    大白话：恢复任务时最重要的是找到任务目录。
    这里尽量从 run_id、task_id、LocalStore 的 subagent source_id 里提取 `subagent-*`。
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

    大白话：这里读的是任务目录事实源。
    archive 只能提示“可能相关”，真正判断完成没完成，要回到 STATUS、WORK_LOG、HANDOFF、TESTS 这些文件。
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
            }
        )
    return payloads


def build_resume_guidance(
    archive_matches: list[dict[str, Any]],
    local_hits: list[dict[str, Any]],
    task_payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    """LLM: summarize recovery clues into next reads and safe next actions.

    大白话：恢复命令不能直接替用户下结论。
    它应该告诉你“找到了哪些线索、先读哪些权威文件、下一步怎么核对”。
    """

    recommended_reads: list[str] = []
    for task in task_payloads:
        for path in task.get("recommended_read_paths", []) or []:
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
    if not task_payloads:
        next_actions.append("如果没有任务目录，先用 memory-archive-search 缩小 request_id/run_id/session_id。")
    return {
        "archive_match_count": len(archive_matches),
        "local_match_count": len(local_hits),
        "task_fact_source_count": len(task_payloads),
        "recommended_read_paths": recommended_reads[:20],
        "next_actions": next_actions,
    }


def strip_sort_keys(payload: Any) -> Any:
    """LLM: remove internal sort keys from JSON output recursively.

    大白话：`created_at_sort` 只是命令内部排序用的数字。
    输出给用户时去掉它，避免用户误以为这是正式业务字段。
    """

    if isinstance(payload, list):
        return [strip_sort_keys(item) for item in payload]
    if isinstance(payload, dict):
        return {key: strip_sort_keys(value) for key, value in payload.items() if key != "created_at_sort"}
    return payload


def _archive_files(root: Path, *, layer: str, date_key: str | None) -> list[tuple[str, Path]]:
    """LLM: return existing archive files for raw/hook layers in newest-first order.

    大白话：如果指定日期，就只看那天的文件；没指定日期，就看最近若干个 JSONL 文件。
    这样不会为了一个 list/search 命令把多年归档一次性翻完。
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

    大白话：目录规则不要散落在 CLI 里。
    hook 用 `snapshot_path_for`，raw 用 `raw_event_path_for`，以后路径变了这里也能跟着变。
    """

    return snapshot_path_for(root).parent if layer == "hook" else raw_event_path_for(root).parent


def _read_archive_file(layer: str, path: Path) -> list[dict[str, Any]]:
    """LLM: parse one archive JSONL file and skip malformed lines without crashing.

    大白话：归档是排障兜底层。
    即使里面有一行坏 JSON，命令也应该继续读其他行，并把坏行标出来，而不是直接中断。
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

    大白话：raw 事件有 event_id、speaker、tool_name；hook 快照有 snapshot_id、user_intents、next_actions。
    统一后，搜索命令就能按同一套字段工作。
    """

    created_at = str(payload.get("created_at", "") or "")
    record_id = str(payload.get("event_id") or payload.get("snapshot_id") or f"{path.name}:{line_no}")
    return {
        "layer": layer,
        "kind": "hook_snapshot" if layer == "hook" else "raw_event",
        "id": record_id,
        "session_id": str(payload.get("session_id", "") or ""),
        "request_id": str(payload.get("request_id", "") or ""),
        "run_id": str(payload.get("run_id", "") or ""),
        "task_id": str(payload.get("task_id", "") or ""),
        "speaker": str(payload.get("speaker", "") or ""),
        "target": str(payload.get("target", "") or ""),
        "action": str(payload.get("action", "snapshot" if layer == "hook" else "") or ""),
        "status": str(payload.get("status", "") or ""),
        "error_code": str(payload.get("error_code", "") or ""),
        "is_dispatch": bool(payload.get("is_dispatch", False)),
        "tool_name": str(payload.get("tool_name", "") or ""),
        "tool_success": payload.get("tool_success"),
        "source": str(payload.get("source", "") or ""),
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


def _archive_preview(payload: dict[str, Any]) -> str:
    """LLM: derive a compact human-readable preview from raw or hook payloads.

    大白话：不同类型的归档正文位置不一样。
    这里优先拿 content_preview；如果没有，就从意图、动作、决策、下一步里拼一个短摘要。
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

    大白话：坏行也算一种线索。
    用户至少应该知道哪个文件第几行坏了，而不是看到命令静悄悄漏掉内容。
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

    大白话：关键词不只搜正文预览，也搜 ID、工具名、状态、任务引用和原始 payload。
    这样用户记得某个 request_id 时也能找回来。
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
    text = str(value or "").strip()
    if not text or "subagent-" not in text:
        return
    run_id = text[text.find("subagent-") :].split()[0].strip("`'\",)")
    if run_id and run_id not in items:
        items.append(run_id)


def _created_at_sort(value: str, *, fallback: float) -> float:
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


def _list_value(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]
