# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""concrete action handlers used by SubAgentActionService.

给人看的解释：
每个 handler 只处理一种动作写回，公共审计字段放在 ActionHandlerContext 里。
"""

from dataclasses import dataclass
from pathlib import Path

from .action_params import RecordAfterTaskActionParams


# LLM: ActionHandlerContext 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存动作handler上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class ActionHandlerContext:
    """Common audit fields for one action application."""

    before_status: str
    before_channel_status: str
    now: float
    take_over_by: str = ""
    locked_files: list[str] | None = None


# LLM: apply_probe_or_repair_channel 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新proberepair通道对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
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


# LLM: apply_repair_work_order 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新repairworkorder对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
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


# LLM: apply_reopen_for_evidence 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新reopen证据对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
def apply_reopen_for_evidence(service, action, task, ctx: ActionHandlerContext):
    task.status = "BLOCKED"
    task.failure_type = "missing_evidence"
    task.verification_status = "UNVERIFIED"
    task.updated_at = ctx.now
    task.result = task.result or "缺少验收证据，等待补充 evidence 后再完成。"
    service.manager.save(task)
    service._append_task_work_log(task, "action_apply reopen_for_evidence: 已重开任务并等待验收证据。")
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action, task, ctx.before_status, ctx.before_channel_status, "已把缺证据的 DONE 任务改为 BLOCKED。"
        )
    )


# LLM: apply_run_acceptance 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新验收对应的任务或运行状态，并保留既有字段语义；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
def apply_run_acceptance(service, action, task, ctx: ActionHandlerContext):
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.updated_at = ctx.now
    service.manager.save(task)
    service._append_task_work_log(task, "action_apply run_acceptance: 已标记为需要验收。")
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action, task, ctx.before_status, ctx.before_channel_status, "已标记为需要验收，未自动执行未知命令。"
        )
    )


# LLM: apply_takeover_or_reassign 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新takeoverreassign对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
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
        RecordAfterTaskActionParams(
            action,
            task,
            ctx.before_status,
            ctx.before_channel_status,
            f"已由 {take_over_by} 接管任务。",
            evidence_paths=[task.takeover_file, task.work_log_file],
        )
    )


# LLM: apply_record_only_action 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新only动作对应的任务或运行状态，并保留既有字段语义；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def apply_record_only_action(service, action, task, ctx: ActionHandlerContext):
    task.updated_at = ctx.now
    service.manager.save(task)
    service._append_task_work_log(task, f"action_apply {action.action}: 已记录待人工处理，不自动修改能力授权。")
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action,
            task,
            ctx.before_status,
            ctx.before_channel_status,
            f"已记录 {action.action} 待人工处理。",
        )
    )
