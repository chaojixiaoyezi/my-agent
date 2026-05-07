# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Patch apply task helper for backward-compatible inline implementation."""

from __future__ import annotations

import time

from agent_py_agent.agent.subagents.reports import PatchApplyRecord


# LLM: PatchApplyTaskHelper 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装补丁应用任务辅助相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class PatchApplyTaskHelper:
    """Handles inline patch apply task logic for backward compatibility.

    This helper is used when _patch_apply_service is not initialized.
    """

    # LLM: apply_patch_task 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 更新补丁任务对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
    @staticmethod
    def apply_patch_task(
        task,
        *,
        params=None,
        output: dict | None = None,
        patches: list[dict] | None = None,
        apply: bool = False,
        applier: str = "parent",
        note: str = "",
    ):
        """Apply patches for a single task (inline minimal implementation)."""
        # LLM: 服务未初始化时，兜底路径仍镜像“参数包优先”的服务流程。
        apply, applier, note, patches = _coerce_apply_patch_inputs(
            params,
            apply=apply,
            applier=applier,
            note=note,
            patches=patches,
        )

        now = time.time()
        blocked, invalid = _blocked_and_invalid_patch_items(patches)
        ok = not blocked and not invalid
        return PatchApplyRecord(
            id=f"apply-{task.id if hasattr(task, 'id') else 'unknown'}-{int(now)}",
            run_id=task.id if hasattr(task, "id") else "",
            dry_run=not apply,
            applied=False,
            ok=ok,
            decision=_patch_apply_decision(patches, ok),
            message=f"{len(patches)} patches, {len(blocked)} blocked, {len(invalid)} invalid",
            patch_count=len(patches),
            applied_count=0,
            blocked_count=len(blocked),
            applier=applier,
            note=note,
            evidence_paths=[
                task.output_json if hasattr(task, "output_json") else "",
                task.work_log_file if hasattr(task, "work_log_file") else "",
            ],
            test_commands=[],
            test_results=[],
            patches=[],
            created_at=now,
        )


# LLM: _coerce_apply_patch_inputs 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 解析并归一化补丁inputs的输入形态，让下游只处理稳定结构；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
def _coerce_apply_patch_inputs(params, *, apply, applier, note, patches):
    if params is None:
        return apply, applier, note, patches or []
    return params.apply, params.applier, params.note, params.patches


# LLM: _blocked_and_invalid_patch_items 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理blockedinvalid补丁条目相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _blocked_and_invalid_patch_items(patches):
    blocked = [
        item for item in patches
        if str(item.get("status", "")).lower() in {"planned", "blocked"}
    ]
    invalid = [
        item for item in patches
        if str(item.get("status", "")).lower() not in {"applied", "planned", "blocked"}
    ]
    return blocked, invalid


# LLM: _patch_apply_decision 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理补丁应用decision相关的数据流，连接当前职责的前后步骤；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
def _patch_apply_decision(patches, ok):
    if not patches:
        return "NO_PATCHES"
    return "APPLY" if ok else "BLOCK"
