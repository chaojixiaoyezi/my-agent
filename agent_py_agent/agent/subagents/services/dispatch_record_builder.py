# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Dispatch record creation and building helpers."""

from __future__ import annotations

import time


# LLM: DispatchRecordBuilder 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装调度记录构建器相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class DispatchRecordBuilder:
    """Build dispatch audit records."""

    # LLM: make_record 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建make记录所需的数据结构或请求参数，包括 auto-policy、auto-execution、follow-up 和 test report 摘要；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    @staticmethod
    def make_record(manager, params) -> DispatchRecord:
        """Create a dispatch audit record from params bundle."""
        from agent_py_agent.agent.subagents.reports import DispatchRecord

        return DispatchRecord(
            id=manager._new_id("dispatch"),
            step=params.step,
            action=params.action,
            run_id=params.run_id,
            dry_run=params.dry_run,
            applied=params.applied,
            ok=params.ok,
            message=params.message,
            before_status=params.before_status,
            after_status=params.after_status,
            before_verification_status=params.before_verification_status,
            after_verification_status=params.after_verification_status,
            evidence_paths=params.evidence_paths or [],
            # LLM: keep child creation refs in the audit record without loading child artifacts.
            # 字段用途: 让父级看到 runner 真实创建的下级数量、id 和角色，后续按 refs 继续处理。
            runner_summary=params.runner_summary,
            runner_created_child_count=params.runner_created_child_count,
            runner_created_child_ids=params.runner_created_child_ids or [],
            runner_created_roles=params.runner_created_roles or [],
            runner_child_status_counts=params.runner_child_status_counts or {},
            runner_unfinished_child_ids=params.runner_unfinished_child_ids or [],
            runner_partial_success=params.runner_partial_success,
            created_at=time.time(),
        )

    # LLM: build_summary 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建buildsummary所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    @staticmethod
    def build_summary(records) -> dict[str, int]:
        """Summarize dispatch audit records."""
        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary[record.step] = summary.get(record.step, 0) + 1
            summary[record.action] = summary.get(record.action, 0) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed", 0,
            ) + 1
            summary["applied" if record.applied else "dry_run"] = summary.get(
                "applied" if record.applied else "dry_run", 0,
            ) + 1
            summary["runner_created_children"] = summary.get(
                "runner_created_children", 0,
            ) + int(record.runner_created_child_count or 0)
        return summary
