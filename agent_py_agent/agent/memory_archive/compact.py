# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

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
from .storage import compression_snapshot_dir
from .tokens import token_ledger_dir


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 MemoryCompactPlanOptions 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MemoryCompactPlanOptions 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class MemoryCompactPlanOptions:
    """参数说明: memory compact dry-run 的过滤条件。"""

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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 build_memory_compact_plan 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build memory compact plan 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def build_memory_compact_plan(root: str | Path, options: MemoryCompactPlanOptions) -> dict[str, Any]:
    """返回说明: 构建只读 compact plan，供 CLI/测试消费。"""

    workspace = Path(root)
    records = _matching_archive_records(workspace, options)
    archive_files = _archive_files_from_records(records)
    snapshots = _snapshot_summary(workspace, options)
    tokens = _token_summary(workspace, options.session_id)
    archive_summary = _archive_summary(records, archive_files)
    risks = _risk_notes(archive_summary, snapshots, tokens)
    return {
        "ok": True,
        "mode": "dry-run",
        "dry_run": True,
        "workspace_root": str(workspace),
        "scope": _scope_payload(options),
        "archive": archive_summary,
        "snapshots": snapshots,
        "tokens": tokens,
        "estimated_compactable_bytes": archive_summary["total_bytes"] + tokens["total_bytes"],
        "risks": risks,
        "recommended_actions": _recommended_actions(archive_summary, snapshots, tokens, risks),
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _matching_archive_records 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 matching archive records 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _archive_files_from_records 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 archive files from records 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _archive_files_from_records(records: list[dict[str, Any]]) -> list[Path]:
    paths = {str(record.get("file_path") or "") for record in records}
    return sorted(Path(path) for path in paths if path)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _archive_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 archive summary 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _snapshot_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 snapshot summary 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _token_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 token summary 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _read_json_files 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 read json files 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _manifest_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 manifest payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _payload_matches_scope 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 payload matches scope 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def _payload_matches_scope(payload: dict[str, Any], options: MemoryCompactPlanOptions) -> bool:
    """返回说明: 让 snapshot 也能按 request/run/task 过滤，避免 dry-run 范围过宽。"""

    checks = {
        "request_id": options.request_id,
        "run_id": options.run_id,
        "task_id": options.task_id,
    }
    return all(not expected or _payload_has_value(payload, field, expected) for field, expected in checks.items())


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _payload_has_value 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 payload has value 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _payload_created_at 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 payload created at 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _payload_created_at(payload: dict[str, Any]) -> str:
    created_at = str(payload.get("created_at") or payload.get("timestamp") or "")
    if created_at:
        return created_at
    turns = payload.get("turns")
    if isinstance(turns, list):
        turn_times = [str(item.get("created_at") or "") for item in turns if isinstance(item, dict)]
        return max([item for item in turn_times if item], default="")
    return ""


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _scope_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 scope payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _risk_notes 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 risk notes 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _recommended_actions 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 recommended actions 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _recommended_actions(
    archive: dict[str, Any],
    snapshots: dict[str, Any],
    tokens: dict[str, Any],
    risks: list[str],
) -> list[str]:
    actions = ["Review this dry-run plan before enabling compact apply."]
    if archive["record_count"]:
        actions.append("Keep original raw/hook files until memory-resume is verified after compact.")
    if snapshots["file_count"]:
        actions.append("Use latest compression snapshots as the first recovery anchor.")
    if tokens["ledger_count"]:
        actions.append("Use token ledgers to choose compact thresholds before automatic compaction.")
    if risks:
        actions.append("Run memory-doctor and local-doctor before applying compact.")
    return actions


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _count_by 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 count by 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _count_by(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = str(record.get(key, "") or "-")
        counts[value] = counts.get(value, 0) + 1
    return counts


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _file_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 file payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _file_payload(path: Path) -> dict[str, Any]:
    return {"path": str(path), "size_bytes": _safe_size(path)}


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _total_size 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 total size 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _total_size(paths: list[Path]) -> int:
    return sum(_safe_size(path) for path in paths)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _safe_size 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 safe size 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _safe_int 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 safe int 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
