# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Patch review task helper for backward-compatible inline implementation."""

from __future__ import annotations

import time
from dataclasses import dataclass

from agent_py_agent.agent.subagents.patch.patch_service import PatchReviewTaskRequest


# LLM: PatchReviewGroups 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存补丁审查groups字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class PatchReviewGroups:
    """Partitioned patch groups used to make a review decision."""

    blocked: list[dict]
    invalid: list[dict]
    applied: list[dict]


# LLM: PatchReviewTaskHelper 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装补丁审查任务辅助相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class PatchReviewTaskHelper:
    """Handles inline patch review task logic for backward compatibility.

    This helper is used when _patch_review_service is not initialized.
    """

    # LLM: review_patch_task 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理审查补丁任务相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    @staticmethod
    def review_patch_task(
        request: PatchReviewTaskRequest,
    ):
        """Review a single task's patches (inline implementation)."""
        # LLM: 兜底路径沿用补丁审查服务的请求包，避免接管流程遗漏审查输入。
        task = request.task
        patches = request.patches
        opts = request.options
        now = time.time()
        groups = _partition_patch_review_items(patches)
        ok = bool(patches) and not groups.blocked and not groups.invalid
        decision, message = _patch_review_decision(patches, groups, ok)

        from agent_py_agent.agent.subagents.reports import PatchReviewRecord

        return PatchReviewRecord(
            id=f"review-{task.id if hasattr(task, 'id') else 'unknown'}-{int(now)}",
            run_id=task.id if hasattr(task, "id") else "",
            dry_run=not opts.apply,
            applied=False,
            ok=ok,
            decision=decision,
            message=message,
            patch_count=len(patches),
            approved_count=len(groups.applied),
            blocked_count=len(groups.blocked),
            reviewer=opts.reviewer,
            note=opts.note,
            evidence_paths=[
                task.output_json if hasattr(task, "output_json") else "",
                task.work_log_file if hasattr(task, "work_log_file") else "",
            ],
            patches=[dict(item) for item in patches],
            created_at=now,
        )


# LLM: _partition_patch_review_items 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 拆分补丁审查条目输入集合，给调度、验收或补丁处理提供分组结果；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _partition_patch_review_items(patches) -> PatchReviewGroups:
    blocked = [
        item
        for item in patches
        if str(item.get("status", "")).lower() in {"planned", "blocked"}
    ]
    invalid = [
        item
        for item in patches
        if str(item.get("status", "")).lower() not in {"applied", "planned", "blocked"}
    ]
    applied = [item for item in patches if str(item.get("status", "")).lower() == "applied"]
    return PatchReviewGroups(blocked, invalid, applied)


# LLM: _patch_review_decision 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理补丁审查decision相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _patch_review_decision(patches, groups: PatchReviewGroups, ok):
    if not patches:
        return "NO_PATCHES", "没有 patch 需要审核。"
    if groups.blocked or groups.invalid:
        parts = []
        if groups.blocked:
            parts.append(f"{len(groups.blocked)} 个 patch 处于 planned/blocked")
        if groups.invalid:
            parts.append(f"{len(groups.invalid)} 个 patch 状态未知")
        return "REJECT", "；".join(parts) + "，不能审核通过。"
    return "APPROVE" if ok else "REJECT", f"{len(groups.applied)} 个 patch 已声明 applied，可审核通过。"
