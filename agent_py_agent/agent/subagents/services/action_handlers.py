from __future__ import annotations

"""LLM: concrete action handlers used by SubAgentActionService.

给人看的解释：
每个 handler 只处理一种动作写回，公共审计字段放在 ActionHandlerContext 里。
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ActionHandlerContext:
    """Common audit fields for one action application."""

    before_status: str
    before_channel_status: str
    now: float
    take_over_by: str = ""
    locked_files: list[str] | None = None


def apply_probe_or_repair_channel(service, action, task, ctx: ActionHandlerContext):
    from ..reports import ActionApplyRecord

    result = service.manager.probe_channel(action.run_id)
    task = service.manager.load(action.run_id)
    return ActionApplyRecord(
        id=service.manager._new_id("apply"), action_id=action.id, run_id=action.run_id, action=action.action,
        dry_run=False, applied=True, ok=True,
        message=f"已执行 channel probe，结果为 {result.channel_status}。",
        before_status=ctx.before_status, after_status=task.status,
        before_channel_status=ctx.before_channel_status, after_channel_status=task.channel_status,
        evidence_paths=[task.channel_probe_file, str(Path(task.logs_dir) / "last_channel_probe.json")],
        created_at=ctx.now,
    )


def apply_repair_work_order(service, action, task, ctx: ActionHandlerContext):
    from ..reports import ActionApplyRecord

    service.manager.save(task)
    validation = service.manager.validate_work_order(action.run_id)
    task = service.manager.load(action.run_id)
    ok = validation.ok
    message = "已补齐标准工单现场。" if ok else f"工单仍缺少 {len(validation.missing)} 个路径。"
    service._append_task_work_log(task, f"action_apply repair_work_order: {message}")
    return ActionApplyRecord(
        id=service.manager._new_id("apply"), action_id=action.id, run_id=action.run_id, action=action.action,
        dry_run=False, applied=ok, ok=ok, message=message,
        before_status=ctx.before_status, after_status=task.status,
        before_channel_status=ctx.before_channel_status, after_channel_status=task.channel_status,
        evidence_paths=[task.task_dir, task.work_log_file], created_at=ctx.now,
    )


def apply_reopen_for_evidence(service, action, task, ctx: ActionHandlerContext):
    task.status = "BLOCKED"
    task.failure_type = "missing_evidence"
    task.verification_status = "UNVERIFIED"
    task.updated_at = ctx.now
    task.result = task.result or "缺少验收证据，等待补充 evidence 后再完成。"
    service.manager.save(task)
    service._append_task_work_log(task, "action_apply reopen_for_evidence: 已重开任务并等待验收证据。")
    return service._record_after_task_action(
        action, task, ctx.before_status, ctx.before_channel_status, "已把缺证据的 DONE 任务改为 BLOCKED。"
    )


def apply_run_acceptance(service, action, task, ctx: ActionHandlerContext):
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.updated_at = ctx.now
    service.manager.save(task)
    service._append_task_work_log(task, "action_apply run_acceptance: 已标记为需要验收。")
    return service._record_after_task_action(
        action, task, ctx.before_status, ctx.before_channel_status, "已标记为需要验收，未自动执行未知命令。"
    )


def apply_takeover_or_reassign(service, action, task, ctx: ActionHandlerContext):
    from ..reports import ActionApplyRecord

    take_over_by = ctx.take_over_by
    if not take_over_by:
        return ActionApplyRecord(
            id=service.manager._new_id("apply"), action_id=action.id, run_id=action.run_id, action=action.action,
            dry_run=False, applied=False, ok=False, message="takeover_or_reassign 需要 --take-over-by。",
            before_status=ctx.before_status, after_status=ctx.before_status,
            before_channel_status=ctx.before_channel_status, after_channel_status=ctx.before_channel_status,
            evidence_paths=[task.task_dir], created_at=ctx.now,
        )
    if task.channel_status != "OK":
        service.manager.probe_channel(action.run_id)
        task = service.manager.load(action.run_id)
    if task.channel_status != "OK":
        return ActionApplyRecord(
            id=service.manager._new_id("apply"), action_id=action.id, run_id=action.run_id, action=action.action,
            dry_run=False, applied=False, ok=False,
            message=f"通道状态为 {task.channel_status}，未接管。请先修复通道。",
            before_status=ctx.before_status, after_status=task.status,
            before_channel_status=ctx.before_channel_status, after_channel_status=task.channel_status,
            evidence_paths=[task.channel_probe_file], created_at=ctx.now,
        )
    service.manager.record_takeover(
        action.run_id,
        take_over_by=take_over_by,
        reason=action.reason,
        locked_files=ctx.locked_files or [],
    )
    task = service.manager.load(action.run_id)
    service._append_task_work_log(task, f"action_apply takeover_or_reassign: 已由 {take_over_by} 接管。")
    return service._record_after_task_action(
        action,
        task,
        ctx.before_status,
        ctx.before_channel_status,
        f"已由 {take_over_by} 接管任务。",
        evidence_paths=[task.takeover_file, task.work_log_file],
    )


def apply_record_only_action(service, action, task, ctx: ActionHandlerContext):
    task.updated_at = ctx.now
    service.manager.save(task)
    service._append_task_work_log(task, f"action_apply {action.action}: 已记录待人工处理，不自动修改能力授权。")
    return service._record_after_task_action(
        action,
        task,
        ctx.before_status,
        ctx.before_channel_status,
        f"已记录 {action.action} 待人工处理。",
    )
