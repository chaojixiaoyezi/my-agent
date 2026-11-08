# LLM: 计划只读来源并生成canonical建议；不修改历史消息、元数据或操作权限。
# 模块用途: 预演记忆压缩范围、风险与恢复建议，不执行apply。
from __future__ import annotations

"""dry-run planning for memory compact without mutating archive files.

新手说明:
这里先只做"压缩预演"。它扫描 raw/hook、compression snapshots 和 token ledger，
输出一份计划给 CLI 展示；不会删除、覆盖或重写任何用户数据。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .query import collect_archive_records, filter_archive_records
from .runtime import ArchiveRunTurnResult
from .storage import compression_snapshot_dir, raw_event_path_for
from .tokens import token_ledger_dir


@dataclass(frozen=True)
class MemoryCompactPlanOptions:
    """ memory compact dry-run 的过滤条件。"""

    layer: str = "all"
    date: str = ""
    since: str = ""
    until: str = ""
    session_id: str = ""
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""
    level: int | None = None
    limit: int = 0
    # 宿主当前回合事实，不是配置；CLI 显式历史计划维持默认读法。
    source_mode: str = "historical"
    archive_result: ArchiveRunTurnResult | None = None
    raw_archive_refs: tuple[dict[str, str], ...] = ()
    compression_snapshot_path: str = ""
    token_ledger: dict[str, int] | None = None


def build_memory_compact_plan(root: str | Path, options: MemoryCompactPlanOptions) -> dict[str, Any]:
    """返回说明: 构建只读 compact plan，供 CLI/测试消费。"""

    workspace = Path(root)
    if options.source_mode == "current_turn":
        archive_summary, snapshots, tokens = _current_turn_sources(workspace, options)
    elif options.source_mode == "historical":
        records = _matching_archive_records(workspace, options)
        archive_files = _archive_files_from_records(records)
        snapshots = _snapshot_summary(workspace, options)
        tokens = _token_summary(workspace, options.session_id)
        archive_summary = _archive_summary(records, archive_files)
    else:
        raise ValueError("unknown_compact_source_mode")
    risks = _risk_notes(archive_summary, snapshots, tokens)
    if options.source_mode == "current_turn":
        risks.append("current-turn sources do not establish complete prior-run coverage")
        if not archive_summary["current_append_complete"]:
            risks.append("current archive append provenance is partial or unknown")
    return {
        "ok": True,
        "mode": "dry-run",
        "dry_run": True,
        "workspace_root": str(workspace),
        "scope": _scope_payload(options),
        "source_scope": {"mode": options.source_mode, "coverage": (
            "partial" if options.source_mode == "current_turn" else "historical"),
            "prior_carry": "unknown" if options.source_mode == "current_turn" else "historical"},
        "archive": archive_summary,
        "snapshots": snapshots,
        "tokens": tokens,
        "estimated_compactable_bytes": archive_summary["total_bytes"] + tokens["total_bytes"],
        "risks": risks,
        "recommended_actions": _recommended_actions(archive_summary, snapshots, tokens, risks),
    }


def _current_turn_sources(root: Path, options: MemoryCompactPlanOptions) -> tuple[dict, dict, dict]:
    """只投影原宿主回执和精确快照；目录根、空回执与未知回执不能互相替代。"""
    receipt = options.archive_result
    supplied = [event.to_dict() for event in receipt.events] if receipt is not None else []
    events = []
    receipt_ids = set(receipt.event_ids) if receipt is not None else set()
    paths = dict.fromkeys([*(str(path) for path in (receipt.write_paths if receipt is not None else ())),
                           *(str(ref.get("path") or "") for ref in options.raw_archive_refs)])
    files: dict[str, dict[str, Any]] = {
        path: {**_file_payload(Path(path)), "event_ids": {}, "content_hashes": []}
        for path in paths if path
    }
    complete = (receipt is not None and receipt.event_count == len(supplied) == len(receipt.event_ids) == len(receipt.content_hashes)
                and len(receipt_ids) == len(supplied) and all(receipt.event_ids) and all(receipt.content_hashes))
    for number, event in enumerate(supplied):
        path = str(raw_event_path_for(root, event.get("created_at")))
        identity = str(event.get("event_id") or "")
        matches = all(not expected or str(event.get(key) or "") == expected for key, expected in (
            ("session_id", options.session_id), ("request_id", options.request_id),
            ("run_id", options.run_id), ("task_id", options.task_id)))
        if path not in files or not identity or not matches or identity not in receipt_ids:
            complete = False
            continue
        events.append(event)
        complete = complete and number < len(receipt.content_hashes) and bool(event.get("content_hash")) and (
            event["content_hash"] == receipt.content_hashes[number])
        files[path]["event_ids"][identity] = None
        files[path]["content_hashes"].append(str(event.get("content_hash") or ""))
    for ref in options.raw_archive_refs:
        path, identity = str(ref.get("path") or ""), str(ref.get("event_id") or "")
        complete = complete and bool(path and identity)
        if path:
            if identity:
                files[path]["event_ids"][identity] = None
    archive = {"record_count": len(events), "file_count": len(files),
        "files": [{**item, "event_ids": list(item["event_ids"])} for item in files.values()],
        "total_bytes": sum(len(json.dumps(event, ensure_ascii=False).encode("utf-8")) for event in events),
        "by_layer": {"raw": len(events)}, "by_archive_level": _count_by(events, "archive_level"),
        "error_count": 0, "events": events, "event_ids": [event["event_id"] for event in events],
        "content_hashes": [event.get("content_hash", "") for event in events],
        "raw_archive_refs": list(options.raw_archive_refs), "receipt_known": receipt is not None,
        "receipt_event_count": receipt.event_count if receipt is not None else None,
        "current_append_complete": bool(complete), "prior_carry": "unknown"}
    path = Path(options.compression_snapshot_path) if options.compression_snapshot_path else None
    payloads, invalid = _read_json_files([path if path.is_absolute() else root / path], options=options) if path else ([], 0)
    snapshots = {"directory": str(compression_snapshot_dir(root)), "file_count": len(payloads),
        "invalid_count": invalid, "total_bytes": sum(item["size_bytes"] for item in payloads), "latest": payloads}
    tokens = {"directory": str(token_ledger_dir(root)), "ledger_count": 0, "invalid_count": 0,
        "turn_count": int(options.token_ledger is not None), "total_bytes": 0, "latest": [],
        "cumulative_tokens": int((options.token_ledger or {}).get("cumulative", 0)),
        "current": dict(options.token_ledger or {}), "known": options.token_ledger is not None}
    return archive, snapshots, tokens


def _matching_archive_records(root: Path, options: MemoryCompactPlanOptions) -> list[dict[str, Any]]:
    filters = {
        key: value
        for key, value in {
            "session_id": options.session_id,
            "request_id": options.request_id,
            "run_id": options.run_id,
            "task_id": options.task_id,
        }.items()
        if value
    }
    records = collect_archive_records(
        root,
        layer=options.layer,
        date_key=options.date or None,
        limit=0,
        level=options.level,
    )
    matches = filter_archive_records(
        records,
        query="",
        filters=filters,
        since=options.since or None,
        until=options.until or None,
        level=options.level,
    )
    return matches[: options.limit] if options.limit > 0 else matches


def _archive_files_from_records(records: list[dict[str, Any]]) -> list[Path]:
    paths = {str(record.get("file_path") or "") for record in records}
    return sorted(Path(path) for path in paths if path)


def _archive_summary(records: list[dict[str, Any]], files: list[Path]) -> dict[str, Any]:
    return {
        "record_count": len(records),
        "file_count": len(files),
        "total_bytes": _total_size(files),
        "by_layer": _count_by(records, "layer"),
        "by_archive_level": _count_by(records, "archive_level"),
        "error_count": sum(1 for record in records if record.get("kind") == "archive_error"),
        "files": [_file_payload(path) for path in files],
    }


def _snapshot_summary(root: Path, options: MemoryCompactPlanOptions) -> dict[str, Any]:
    directory = compression_snapshot_dir(root)
    files = sorted(directory.glob("*.json")) if directory.exists() else []
    payloads, invalid = _read_json_files(files, options=options)
    return {
        "directory": str(directory),
        "file_count": len(payloads),
        "invalid_count": invalid,
        "total_bytes": _total_size([Path(item["file_path"]) for item in payloads]),
        "latest": payloads[:5],
    }


def _token_summary(root: Path, session_id: str) -> dict[str, Any]:
    directory = token_ledger_dir(root)
    files = sorted(directory.glob("*.json")) if directory.exists() else []
    payloads, invalid = _read_json_files(files, session_id=session_id)
    total_turns = sum(int(item.get("turn_count", 0) or 0) for item in payloads)
    cumulative = sum(int(item.get("cumulative_tokens", 0) or 0) for item in payloads)
    return {
        "directory": str(directory),
        "ledger_count": len(payloads),
        "invalid_count": invalid,
        "turn_count": total_turns,
        "cumulative_tokens": cumulative,
        "total_bytes": _total_size([Path(item["file_path"]) for item in payloads]),
        "latest": payloads[:5],
    }


def _read_json_files(
    files: list[Path],
    *,
    session_id: str = "",
    options: MemoryCompactPlanOptions | None = None,
) -> tuple[list[dict[str, Any]], int]:
    payloads: list[dict[str, Any]] = []
    invalid = 0
    effective_session_id = options.session_id if options else session_id
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            invalid += 1
            continue
        if not isinstance(payload, dict):
            invalid += 1
            continue
        if effective_session_id and str(payload.get("session_id") or "") != effective_session_id:
            continue
        if options and not _payload_matches_scope(payload, options):
            continue
        try:
            payloads.append(_manifest_payload(path, payload))
        except (TypeError, ValueError):
            invalid += 1
    payloads.sort(key=lambda item: (item.get("created_at", ""), item["file_path"]), reverse=True)
    return payloads, invalid


def _manifest_payload(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_path": str(path),
        "size_bytes": _safe_size(path),
        "session_id": str(payload.get("session_id", "") or ""),
        "snapshot_id": str(payload.get("snapshot_id", "") or ""),
        "turn_count": _safe_int(payload.get("turn_count", 0)),
        "cumulative_tokens": _safe_int(payload.get("cumulative_tokens", 0)),
        "created_at": _payload_created_at(payload),
    }


def _payload_matches_scope(payload: dict[str, Any], options: MemoryCompactPlanOptions) -> bool:
    """返回说明: 让 snapshot 也能按 request/run/task 过滤，避免 dry-run 范围过宽。"""

    checks = {
        "request_id": options.request_id,
        "run_id": options.run_id,
        "task_id": options.task_id,
    }
    return all(not expected or _payload_has_value(payload, field, expected) for field, expected in checks.items())


def _payload_has_value(payload: dict[str, Any], field: str, expected: str) -> bool:
    if str(payload.get(field) or "") == expected:
        return True
    turn_range = payload.get("turn_range")
    if isinstance(turn_range, dict) and str(turn_range.get(field) or "") == expected:
        return True
    for event in payload.get("dispatch_events", []):
        if isinstance(event, dict) and str(event.get(field) or "") == expected:
            return True
    if field in {"run_id", "task_id"}:
        refs = payload.get("task_refs", [])
        if isinstance(refs, list | tuple | set):
            return expected in {str(item) for item in refs}
    return False


def _payload_created_at(payload: dict[str, Any]) -> str:
    created_at = str(payload.get("created_at") or payload.get("timestamp") or "")
    if created_at:
        return created_at
    turns = payload.get("turns")
    if isinstance(turns, list):
        turn_times = [str(item.get("created_at") or "") for item in turns if isinstance(item, dict)]
        return max([item for item in turn_times if item], default="")
    return ""


def _scope_payload(options: MemoryCompactPlanOptions) -> dict[str, Any]:
    return {
        "layer": options.layer,
        "date": options.date,
        "since": options.since,
        "until": options.until,
        "session_id": options.session_id,
        "request_id": options.request_id,
        "run_id": options.run_id,
        "task_id": options.task_id,
        "level": options.level,
        "limit": options.limit,
    }


def _risk_notes(archive: dict[str, Any], snapshots: dict[str, Any], tokens: dict[str, Any]) -> list[str]:
    risks: list[str] = []
    if archive["error_count"]:
        risks.append("archive contains unreadable or invalid JSONL records")
    if snapshots["invalid_count"]:
        risks.append("compression snapshot directory contains invalid JSON files")
    if tokens["invalid_count"]:
        risks.append("token ledger directory contains invalid JSON files")
    if archive["record_count"] and not snapshots["file_count"]:
        risks.append("archive records exist but no matching authoritative snapshot was found")
    return risks


# LLM: 只替换本次新生成的建议，风险、原档保留和Doctor前置合同不变。
# 函数用途: 为压缩预演列出检查与恢复入口，不执行命令。
def _recommended_actions(
    archive: dict[str, Any],
    snapshots: dict[str, Any],
    tokens: dict[str, Any],
    risks: list[str],
) -> list[str]:
    actions = ["Review this dry-run plan before enabling compact apply."]
    if archive["record_count"]:
        actions.append("Keep original raw/hook files until my-agent memory archive resume is verified after compact.")
    if snapshots["file_count"]:
        actions.append("Use latest compression snapshots as the first recovery anchor.")
    if tokens["ledger_count"]:
        actions.append("Use token ledgers to choose compact thresholds before automatic compaction.")
    if risks:
        actions.append("Run my-agent memory doctor and local-doctor before applying compact.")
    return actions


def _count_by(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = str(record.get(key, "") or "-")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _file_payload(path: Path) -> dict[str, Any]:
    return {"path": str(path), "size_bytes": _safe_size(path)}


def _total_size(paths: list[Path]) -> int:
    return sum(_safe_size(path) for path in paths)


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
