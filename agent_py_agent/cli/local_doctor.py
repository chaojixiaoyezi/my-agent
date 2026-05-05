from __future__ import annotations

"""LLM: builds LocalStore/gateway/subagent diagnostics and rebuild plans.

给人看的解释：
这里负责'体检'和'重建索引'的业务规则。
命令函数只负责打印，真正判断哪里坏了、该建议什么修复动作，都放在这里。
"""

import json
import sys
from pathlib import Path

from ..agent.core import SimpleAgent
from ..agent.gateway import (
    gateway_paths,
    gateway_request_counts,
    gateway_stale_processing,
    rebuild_gateway_index,
)
from .common import _memory_record_count


def _add_doctor_check(
    checks: list[dict],
    *,
    name: str,
    ok: bool,
    severity: str,
    message: str,
    details: dict | None = None,
) -> None:
    checks.append(
        {
            "name": name,
            "ok": ok,
            "severity": "ok" if ok else severity,
            "message": message,
            "details": details or {},
        }
    )



def _check_local_store_open(checks: list[dict], stats: dict) -> None:
    """Add local_store_open check."""
    _add_doctor_check(
        checks,
        name="local_store_open",
        ok=True,
        severity="P0",
        message="LocalStore SQLite 可以打开。",
        details=stats,
    )


def _check_memory_index(checks: list[dict], suggestions: list[str], memory_count: int, memory_indexed: int) -> None:
    """Add memory_index check."""
    memory_ok = memory_count == memory_indexed
    _add_doctor_check(
        checks,
        name="memory_index",
        ok=memory_ok,
        severity="P1",
        message=f"JSONL 记忆 {memory_count} 条，LocalStore memory 索引 {memory_indexed} 条。",
        details={"memory_jsonl": memory_count, "memory_indexed": memory_indexed},
    )
    if not memory_ok:
        suggestions.append("运行 `my-agent local-rebuild --source memory` 补齐记忆索引。")


def _check_content_files(checks: list[dict], suggestions: list[str], missing_files: list, limit: int) -> None:
    """Add content files check."""
    _add_doctor_check(
        checks,
        name="local_store_content_files",
        ok=not missing_files,
        severity="P1",
        message=f"LocalStore 正文文件缺失 {len(missing_files)} 条。",
        details={"missing": missing_files},
    )
    if missing_files:
        suggestions.append("运行 `my-agent local-rebuild --reset` 从原始文件事实源重建索引。")


def _check_gateway_queue(checks: list[dict], suggestions: list[str], paths, counts: dict, agent) -> None:
    """Add gateway queue check."""
    stale_processing = gateway_stale_processing(paths, agent.config.gateway_processing_timeout_seconds)
    _add_doctor_check(
        checks,
        name="gateway_queue",
        ok=not stale_processing,
        severity="P1",
        message=f"gateway 队列 {json.dumps(counts, ensure_ascii=False, sort_keys=True)}；stale processing={len(stale_processing)}。",
        details={"counts": counts, "stale_processing": stale_processing},
    )
    if stale_processing:
        suggestions.append("运行 `my-agent gateway restart --force` 或 `my-agent local-doctor --repair` 处理卡住的 processing 请求。")


def _check_work_orders(checks: list[dict], suggestions: list[str], agent, limit: int) -> None:
    """Add subagent work orders check."""
    invalid_work_orders = []
    for task in agent.subagents.list_runs():
        validation = agent.subagents.validate_work_order(task.id)
        if not validation.ok:
            invalid_work_orders.append(
                {"run_id": task.id, "missing": validation.missing, "warnings": validation.warnings}
            )
            if len(invalid_work_orders) >= limit:
                break
    _add_doctor_check(
        checks,
        name="subagent_work_orders",
        ok=not invalid_work_orders,
        severity="P1",
        message=f"subagent 工单缺失关键文件 {len(invalid_work_orders)} 条。",
        details={"invalid": invalid_work_orders},
    )
    if invalid_work_orders:
        suggestions.append("运行 `my-agent subagents-apply-actions --apply --action repair_work_order` 修复缺失工单文件。")


def build_local_doctor_report(agent: SimpleAgent, *, limit: int = 20) -> dict:
    """生成本地事实源/gateway/subagent 的诊断报告。"""

    paths = gateway_paths(agent)
    checks: list[dict] = []
    suggestions: list[str] = []
    stats = agent.local_store.stats()
    source_counts = agent.local_store.source_counts()
    memory_count = _memory_record_count(agent)
    memory_indexed = source_counts.get("memory", 0)
    _check_local_store_open(checks, stats)
    for label, path in {
        "memory_path": agent.memory.path,
        "local_store_db": Path(stats["db_path"]),
        "local_store_files_dir": Path(stats["files_dir"]),
        "local_store_events_path": Path(stats["events_path"]),
        "gateway_workspace": paths.root,
        "subagent_workspace": agent.subagents.workspace,
    }.items():
        exists = path.exists()
        _add_doctor_check(
            checks,
            name=f"path_{label}",
            ok=exists or label in {"memory_path", "gateway_workspace", "subagent_workspace"},
            severity="P1",
            message=f"{label} {'存在' if exists else '尚未创建'}: {path}",
            details={"path": str(path), "exists": exists},
        )
    _check_memory_index(checks, suggestions, memory_count, memory_indexed)
    missing_files = agent.local_store.missing_content_files(limit=limit)
    _check_content_files(checks, suggestions, missing_files, limit)
    counts = gateway_request_counts(paths)
    _check_gateway_queue(checks, suggestions, paths, counts, agent)
    _check_work_orders(checks, suggestions, agent, limit)
    if stats["record_count"] == 0 and (memory_count or agent.subagents.list_runs() or any(counts.values())):
        suggestions.append("LocalStore 为空但磁盘上已有事实源，建议运行 `my-agent local-rebuild`。")
    ok = all(item["ok"] for item in checks)
    return {
        "ok": ok,
        "stats": stats,
        "source_counts": source_counts,
        "memory_count": memory_count,
        "gateway_counts": counts,
        "checks": checks,
        "suggestions": suggestions,
    }



def rebuild_subagent_index(agent: SimpleAgent) -> int:
    """从 subagent 工单目录重建主要 LocalStore 记录。"""

    count = 0
    for task in agent.subagents.list_runs():
        agent.subagents._index_task(task)
        count += 1
        task_dir = Path(task.task_dir)
        for rel_path, source_type in {
            "WORK_LOG.md": "subagent_work_log",
            "execution_context.json": "subagent_execution_context",
            "reports/runner_result.json": "subagent_runner_result",
            "reports/acceptance_review.json": "subagent_acceptance_review",
            "reports/patch_review.json": "subagent_patch_review",
        }.items():
            path = task_dir / rel_path
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            source_id = task.id if rel_path == "execution_context.json" else f"{task.id}:{rel_path}"
            agent.local_store.log_record(
                source_type=source_type,
                source_id=source_id,
                title=f"{source_type} {task.id}",
                content=content,
                metadata={"run_id": task.id, "path": str(path), "rebuilt": True},
                event_type=f"{source_type}_rebuilt",
            )
            count += 1
    return count


def rebuild_local_store(agent: SimpleAgent, *, sources: set[str], reset: bool = False) -> dict:
    """按来源重建 LocalStore。"""

    if reset:
        agent.local_store.reset()
    result = {
        "reset": reset,
        "sources": sorted(sources),
        "memory_indexed": 0,
        "gateway_indexed": 0,
        "subagent_indexed": 0,
        "fts_rebuilt": 0,
    }
    if "memory" in sources:
        result["memory_indexed"] = agent.memory.index_all()
    if "gateway" in sources:
        result["gateway_indexed"] = rebuild_gateway_index(agent)
    if "subagent" in sources:
        result["subagent_indexed"] = rebuild_subagent_index(agent)
    if "fts" in sources or reset:
        result["fts_rebuilt"] = agent.local_store.rebuild_fts()
    result["stats"] = agent.local_store.stats()
    result["source_counts"] = agent.local_store.source_counts()
    return result


def build_status_suggestions(agent: SimpleAgent, payload: dict) -> list[str]:
    """根据 status payload 给出下一步建议。"""

    suggestions: list[str] = []
    gateway = payload["gateway"]
    counts = gateway["request_counts"]
    local_store = payload["local_store"]
    hot_count = int(payload["subagents"]["hot_count"])
    if gateway["status"] == "stopped" and (counts.get("pending", 0) or counts.get("processing", 0)):
        suggestions.append("gateway 停止但队列里还有请求：运行 `my-agent gateway start`。")
    elif gateway["status"] == "stopped":
        suggestions.append("需要后台值班时运行 `my-agent gateway start`，或直接执行 `my-agent`。")
    if gateway["status"] == "stale":
        suggestions.append("gateway heartbeat 已过期：运行 `my-agent gateway restart --force`。")
    if counts.get("failed", 0):
        suggestions.append("gateway failed 队列非空：运行 `my-agent gateway logs` 和 `my-agent timeline --source-type gateway_request` 排查。")
    if local_store["record_count"] == 0 and (_memory_record_count(agent) or agent.subagents.list_runs() or any(counts.values())):
        suggestions.append("LocalStore 为空但已有文件事实源：运行 `my-agent local-rebuild`。")
    if hot_count:
        suggestions.append("存在红灯 subagent：运行 `my-agent subagents-due-check`，必要时再 `my-agent subagents-dispatch --apply`。")
    if not payload["timeline"] and local_store["record_count"]:
        suggestions.append("LocalStore 有记录但 timeline 为空：运行 `my-agent local-rebuild --reset` 从文件事实源重建事件索引。")
    return suggestions
