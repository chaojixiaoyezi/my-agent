# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Dispatch watch record and report helpers."""

from __future__ import annotations

import time

from .dispatch_params import DispatchWatchRecordParams


# LLM: DispatchWatchBuilder 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装调度监控构建器相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class DispatchWatchBuilder:
    """Build dispatch watch records and reports."""

    # LLM: make_record 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建make记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    @staticmethod
    def make_record(manager, *, params: DispatchWatchRecordParams) -> DispatchWatchRecord:
        """Create a watch loop record."""
        from agent_py_agent.agent.subagents.reports import DispatchWatchRecord

        return DispatchWatchRecord(
            id=manager._new_id("watch"),
            cycle=params.cycle,
            dry_run=params.dry_run,
            ok=params.ok,
            message=params.message,
            dispatch_record_count=params.dispatch_record_count,
            dispatch_summary=params.dispatch_summary or {},
            started_at=params.started_at,
            ended_at=params.ended_at,
            evidence_paths=params.evidence_paths or [],
        )

    # LLM: build_summary 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建buildsummary所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    @staticmethod
    def build_summary(records) -> dict[str, int]:
        """Summarize watch loop records."""
        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed", 0,
            ) + 1
            summary["dry_run" if record.dry_run else "applied"] = summary.get(
                "dry_run" if record.dry_run else "applied", 0,
            ) + 1
            summary["dispatch_records"] = summary.get("dispatch_records", 0) + record.dispatch_record_count
        return summary