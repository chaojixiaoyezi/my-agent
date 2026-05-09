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
    # 函数用途: 构建make记录所需的数据结构或请求参数，包括 auto-policy 和 auto-execution 摘要；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
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
            parent_acceptance_policy_ref=params.parent_acceptance_policy_ref,
            parent_acceptance_policy_decision=params.parent_acceptance_policy_decision,
            parent_acceptance_policy_action=params.parent_acceptance_policy_action,
            parent_acceptance_policy_would_execute=params.parent_acceptance_policy_would_execute,
            parent_acceptance_policy_executed=params.parent_acceptance_policy_executed,
            parent_acceptance_policy_execution_mode=params.parent_acceptance_policy_execution_mode,
            parent_acceptance_policy_automatic_execution_allowed=(
                params.parent_acceptance_policy_automatic_execution_allowed
            ),
            parent_acceptance_policy_recommended_command=params.parent_acceptance_policy_recommended_command,
            parent_acceptance_policy_preflight_status=params.parent_acceptance_policy_preflight_status,
            parent_acceptance_policy_ready_for_automatic_execution=(
                params.parent_acceptance_policy_ready_for_automatic_execution
            ),
            parent_acceptance_policy_preflight_blockers=(
                params.parent_acceptance_policy_preflight_blockers or []
            ),
            parent_acceptance_auto_execution_ref=params.parent_acceptance_auto_execution_ref,
            parent_acceptance_auto_execution_status=params.parent_acceptance_auto_execution_status,
            parent_acceptance_auto_execution_allowed=params.parent_acceptance_auto_execution_allowed,
            parent_acceptance_auto_execution_executed=params.parent_acceptance_auto_execution_executed,
            parent_acceptance_auto_execution_guard_status=params.parent_acceptance_auto_execution_guard_status,
            parent_acceptance_auto_execution_blocked_by=(
                params.parent_acceptance_auto_execution_blocked_by or []
            ),
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
        return summary
