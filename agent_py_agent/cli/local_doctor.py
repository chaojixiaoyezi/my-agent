from __future__ import annotations

# LLM: CLI诊断只调用既有领域维护入口；子代理重建不在此解析TTL或事实文件，实际LocalStore显式透传。
# 模块用途: 汇总体检与修复建议，并把索引维护交给各自所属领域。

"""builds LocalStore/gateway/subagent diagnostics and rebuild plans.

给人看的解释：
这里负责'体检'与修复建议，重建索引委托所属领域服务。
命令函数只负责打印；子代理TTL和事实文件回填由索引服务维护。
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from ..agent.core import SimpleAgent
from ..agent.gateway_parts import (
    gateway_paths,
    gateway_request_counts,
    gateway_stale_processing,
    rebuild_gateway_index,
)
from .common import _memory_record_count


@dataclass(frozen=True)
class DoctorCheckRequest:
    name: str
    ok: bool
    severity: str
    message: str
    details: dict | None = None


@dataclass(frozen=True)
class GatewayQueueCheckContext:
    paths: object
    counts: dict
    agent: object


def _add_doctor_check(checks: list[dict], request: DoctorCheckRequest) -> None:
    checks.append(
        {
            "name": request.name,
            "ok": request.ok,
            "severity": "ok" if request.ok else request.severity,
            "message": request.message,
            "details": request.details or {},
        }
    )



def _check_local_store_open(checks: list[dict], stats: dict) -> None:
    _add_doctor_check(
        checks,
        DoctorCheckRequest(
            name="local_store_open",
            ok=True,
            severity="P0",
            message="LocalStore SQLite 可以打开。",
            details=stats,
        ),
    )


# LLM: coverage由正式读索引按当前来源签名、owner及代次核验；此处只展示事实，不修复或恢复旧正文投影。
# 函数用途: 仅将完整同步的正式记忆读索引判为健康，并为未同步状态提示显式重建。
def _check_memory_index(checks: list[dict], suggestions: list[str], memory_count: int, coverage: dict) -> None:
    memory_ok = coverage["status"] == "complete"
    memory_indexed = coverage.get("indexed_records")
    _add_doctor_check(
        checks,
        DoctorCheckRequest(
            name="memory_index",
            ok=memory_ok,
            severity="P1",
            message=f"正式记忆 {memory_count} 条，MemoryReadIndex 已覆盖 {memory_indexed if memory_indexed is not None else '未知'} 条，coverage={coverage['status']}。",
            details={"memory_jsonl": memory_count, "memory_indexed": memory_indexed, "coverage": coverage},
        ),
    )
    if not memory_ok:
        suggestions.append("运行 `my-agent local-rebuild --source memory` 追平正式记忆读索引；coverage=complete 后恢复健康。")


def _check_content_files(checks: list[dict], suggestions: list[str], missing_files: list, limit: int) -> None:
    _add_doctor_check(
        checks,
        DoctorCheckRequest(
            name="local_store_content_files",
            ok=not missing_files,
            severity="P1",
            message=f"LocalStore 正文文件缺失 {len(missing_files)} 条。",
            details={"missing": missing_files},
        ),
    )
    if missing_files:
        suggestions.append("运行 `my-agent local-rebuild --reset` 从原始文件事实源重建索引。")


def _check_gateway_queue(checks: list[dict], suggestions: list[str], ctx: GatewayQueueCheckContext) -> None:
    stale_processing = gateway_stale_processing(ctx.paths, ctx.agent.config.gateway_processing_timeout_seconds)
    _add_doctor_check(
        checks,
        DoctorCheckRequest(
            name="gateway_queue",
            ok=not stale_processing,
            severity="P1",
            message=f"gateway 队列 {json.dumps(ctx.counts, ensure_ascii=False, sort_keys=True)}；stale processing={len(stale_processing)}。",
            details={"counts": ctx.counts, "stale_processing": stale_processing},
        ),
    )
    if stale_processing:
        suggestions.append("运行 `my-agent gateway restart --force` 或 `my-agent local-doctor --repair` 处理卡住的 processing 请求。")


def _check_work_orders(checks: list[dict], suggestions: list[str], agent, limit: int) -> None:
    invalid_work_orders = _collect_invalid_work_orders(agent, limit)
    _add_doctor_check(
        checks,
        DoctorCheckRequest(
            name="subagent_work_orders",
            ok=not invalid_work_orders,
            severity="P1",
            message=f"subagent 工单缺失关键文件 {len(invalid_work_orders)} 条。",
            details={"invalid": invalid_work_orders},
        ),
    )
    if invalid_work_orders:
        suggestions.append("运行 `my-agent subagents-apply-actions --apply --action repair_work_order` 修复缺失工单文件。")


def _collect_invalid_work_orders(agent, limit: int) -> list[dict]:
    invalid_work_orders = []
    for task in agent.subagents.list_runs():
        validation = agent.subagents.validate_work_order(task.id)
        if validation.ok:
            continue
        invalid_work_orders.append({"run_id": task.id, "missing": validation.missing, "warnings": validation.warnings})
        if len(invalid_work_orders) >= limit:
            break
    return invalid_work_orders


def _doctor_workspace_scope(agent: SimpleAgent, mode: str | None, suggestions: list[str]) -> dict[str, str]:
    configured = str(getattr(agent.config, "workspace_root", "") or "").strip()
    scope_mode = mode or ("configured" if configured else "implicit_cwd")
    if scope_mode == "implicit_cwd":
        suggestions.append(
            "当前诊断绑定命令调用目录；诊断常驻服务实例时请传 "
            "`--workspace-root <service WorkingDirectory>`，避免检查到另一套工作区事实源。"
        )
    return {
        "workspace_root": str(agent.root),
        "mode": scope_mode,
        "configured_value": configured,
    }


def _check_doctor_paths(checks: list[dict], agent: SimpleAgent, stats: dict, paths: object) -> None:
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
            DoctorCheckRequest(
                name=f"path_{label}",
                ok=exists or label in {"memory_path", "gateway_workspace", "subagent_workspace"},
                severity="P1",
                message=f"{label} {'存在' if exists else '尚未创建'}: {path}",
                details={"path": str(path), "exists": exists},
            ),
        )


# LLM: 正式记忆覆盖沿领域健康快照消费MemoryReadIndex，LocalStore仅描述工作区来源；不隐式推进索引。
# 函数用途: 汇总当前工作区和owner正式记忆的真实健康事实及修复建议。
def build_local_doctor_report(
    agent: SimpleAgent,
    *,
    limit: int = 20,
    workspace_root_mode: str | None = None,
) -> dict:

    paths = gateway_paths(agent)
    checks: list[dict] = []
    suggestions: list[str] = []
    workspace_scope = _doctor_workspace_scope(agent, workspace_root_mode, suggestions)
    stats = agent.local_store.stats()
    source_counts = agent.local_store.source_counts()
    memory_count = _memory_record_count(agent)
    memory_coverage = agent.memory.runtime_snapshot().get("read_index_coverage", {"status": "unavailable"})
    _check_local_store_open(checks, stats)
    _check_doctor_paths(checks, agent, stats, paths)
    _check_memory_index(checks, suggestions, memory_count, memory_coverage)
    missing_files = agent.local_store.missing_content_files(limit=limit)
    _check_content_files(checks, suggestions, missing_files, limit)
    counts = gateway_request_counts(paths)
    _check_gateway_queue(checks, suggestions, GatewayQueueCheckContext(paths, counts, agent))
    _check_work_orders(checks, suggestions, agent, limit)
    if stats["record_count"] == 0 and (agent.subagents.list_runs() or any(counts.values())):
        suggestions.append("LocalStore 为空但磁盘上已有事实源，建议运行 `my-agent local-rebuild`。")
    ok = all(item["ok"] for item in checks)
    return {
        "ok": ok,
        "workspace_scope": workspace_scope,
        "stats": stats,
        "source_counts": source_counts,
        "memory_count": memory_count,
        "gateway_counts": counts,
        "checks": checks,
        "suggestions": suggestions,
    }



# LLM: CLI沿既有subagents.indexing公开维护入口，显式保留agent.local_store，不绑定替代manager或吞异常。
# 函数用途: 请求子代理领域重建索引并返回其准确计数。
def rebuild_subagent_index(agent: SimpleAgent) -> int:
    return agent.subagents.indexing.rebuild_subagent_index(local_store=agent.local_store)


def rebuild_local_store(agent: SimpleAgent, *, sources: set[str], reset: bool = False) -> dict:

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


# LLM: Suggestions may expose observation and repair commands, but child execution stays host-owned.
# 函数用途: 根据本地健康事实给普通用户可执行建议，不要求用户手动催跑子代理。
def build_status_suggestions(agent: SimpleAgent, payload: dict) -> list[str]:

    suggestions: list[str] = []
    gateway = payload["gateway"]
    counts = gateway["request_counts"]
    archive_counts = gateway.get("archive_request_counts", {})
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
    if local_store["record_count"] == 0 and (_memory_record_count(agent) or agent.subagents.list_runs() or any(counts.values()) or any(archive_counts.values())):
        suggestions.append("LocalStore 为空但已有文件事实源：运行 `my-agent local-rebuild`。")
    if hot_count:
        suggestions.append("存在红灯 subagent：运行 `my-agent subagents-due-check` 查看事实；启动和可恢复重试由系统自动处理。")
    if not payload["timeline"] and local_store["record_count"]:
        suggestions.append("LocalStore 有记录但 timeline 为空：运行 `my-agent local-rebuild --reset` 从文件事实源重建事件索引。")
    return suggestions
