
from __future__ import annotations

"""takeover and reassignment handlers for subagent action apply."""

from ...role_templates import role_template_snapshot_for_task
from ..takeover.readiness import takeover_readiness_ref_order
from .context import (
    ActionHandlerContext,
    CoordinatorHandoffErrorRequest,
    coordinator_handoff_error_record,
)
from .params import RecordAfterTaskActionParams


def apply_takeover_or_reassign(service, action, task, ctx: ActionHandlerContext):
    if _needs_coordinator_handoff_action(task):
        return coordinator_handoff_error_record(
            CoordinatorHandoffErrorRequest(
                service,
                action,
                task,
                ctx,
                (
                    "带子任务的 coordinator/leader 不能走普通 takeover_or_reassign；"
                    "请使用 recover_coordinator_leadership 并传入 --take-over-by 新 leader。"
                ),
            )
        )
    if _should_create_takeover_run(action, task):
        return _apply_takeover_run(service, action, task, ctx)
    return _apply_explicit_reassign(service, action, task, ctx)


def _apply_takeover_run(service, action, task, ctx: ActionHandlerContext):
    from ..takeover.run import TakeoverRunRequest

    result = service.manager.create_takeover_run(
        TakeoverRunRequest(source_run_id=action.run_id, reason=action.reason)
    )
    task = service.manager.load(action.run_id)
    message = _takeover_apply_message(result)
    service._append_task_work_log(task, f"action_apply takeover_or_reassign: {message}")
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action,
            task,
            ctx.before_status,
            ctx.before_channel_status,
            message,
            evidence_paths=_takeover_action_evidence(service, task, result),
        )
    )


def _apply_explicit_reassign(service, action, task, ctx: ActionHandlerContext):
    from ...reports import ActionApplyRecord

    take_over_by = ctx.take_over_by
    if not take_over_by:
        return _missing_takeover_target_record(service, action, task, ctx)
    if task.channel_status != "OK":
        service.manager.probe_channel(action.run_id)
        task = service.manager.load(action.run_id)
    if task.channel_status != "OK":
        return _channel_blocked_takeover_record(service, action, task, ctx)
    service.manager.record_takeover(
        action.run_id,
        take_over_by=take_over_by,
        reason=action.reason,
        locked_files=ctx.locked_files or [],
    )
    task = service.manager.load(action.run_id)
    service._append_task_work_log(task, f"action_apply takeover_or_reassign: 已由 {take_over_by} 接管。")
    evidence_paths = takeover_readiness_ref_order(task.takeover_readiness_json)
    evidence_paths.extend([task.takeover_file, task.work_log_file])
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action,
            task,
            ctx.before_status,
            ctx.before_channel_status,
            f"已由 {take_over_by} 接管任务。",
            evidence_paths=list(dict.fromkeys(ref for ref in evidence_paths if ref)),
        )
    )


def _missing_takeover_target_record(service, action, task, ctx: ActionHandlerContext):
    from ...reports import ActionApplyRecord

    return ActionApplyRecord(
        id=service.manager._new_id("apply"),
        action_id=action.id,
        run_id=action.run_id,
        action=action.action,
        dry_run=False,
        applied=False,
        ok=False,
        message="takeover_or_reassign 需要 --take-over-by。",
        before_status=ctx.before_status,
        after_status=ctx.before_status,
        before_channel_status=ctx.before_channel_status,
        after_channel_status=ctx.before_channel_status,
        evidence_paths=[task.task_dir],
        created_at=ctx.now,
    )


def _channel_blocked_takeover_record(service, action, task, ctx: ActionHandlerContext):
    from ...reports import ActionApplyRecord

    return ActionApplyRecord(
        id=service.manager._new_id("apply"),
        action_id=action.id,
        run_id=action.run_id,
        action=action.action,
        dry_run=False,
        applied=False,
        ok=False,
        message=f"通道状态为 {task.channel_status}，未接管。请先修复通道。",
        before_status=ctx.before_status,
        after_status=task.status,
        before_channel_status=ctx.before_channel_status,
        after_channel_status=task.channel_status,
        evidence_paths=[task.channel_probe_file],
        created_at=ctx.now,
    )


def _needs_coordinator_handoff_action(task) -> bool:
    if not getattr(task, "child_ids", None):
        return False
    if not bool(role_template_snapshot_for_task(task).get("can_spawn_children")):
        return False
    status = str(getattr(task, "status", "") or "").upper()
    failure_type = str(getattr(task, "failure_type", "") or "").lower()
    return status in {"TIMEOUT", "CHANNEL_ERROR"} or failure_type in {
        "runner_timeout",
        "channel_error",
        "runner_channel_failed",
    }


def _should_create_takeover_run(action, task) -> bool:
    triggers = {item for item in str(getattr(action, "rescue_trigger", "") or "").split(",") if item}
    if triggers & {"run_timeout", "heartbeat_stale", "status_timeout"}:
        return True
    status = str(getattr(task, "status", "") or "").upper()
    failure_type = str(getattr(task, "failure_type", "") or "").lower()
    return status in {"TIMEOUT", "CHANNEL_ERROR"} or failure_type in {
        "runner_timeout",
        "channel_error",
        "runner_channel_failed",
    }


def _takeover_action_evidence(service, task, result) -> list[str]:
    refs = [task.takeover_file, task.work_log_file]
    try:
        refs.append(service.manager.load(result.takeover_run_id).task_dir)
    except FileNotFoundError:
        pass
    refs.extend(str(value) for value in result.takeover_refs.values())
    return list(dict.fromkeys(ref for ref in refs if ref))


def _takeover_apply_message(result) -> str:
    if result.created and result.takeover_run_id:
        return f"已创建 takeover run {result.takeover_run_id} 接管原任务。"
    if result.takeover_run_id:
        return f"未新建 takeover run；已复用 {result.takeover_run_id}。"
    return f"未新建 takeover run：{result.message}"
