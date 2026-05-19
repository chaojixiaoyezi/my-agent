# LLM: Takeover action handlers stay isolated because replacement-run recovery has its own lifecycle.
# 模块用途: 处理普通任务接管、replacement run 创建和 takeover apply 证据，不混入其他 action handler。

from __future__ import annotations

"""takeover and reassignment handlers for subagent action apply."""

from .action_context import (
    ActionHandlerContext,
    CoordinatorHandoffErrorRequest,
    coordinator_handoff_error_record,
)
from .action_params import RecordAfterTaskActionParams
from .takeover_readiness import takeover_readiness_ref_order


# LLM: apply_takeover_or_reassign handles replacement runs or explicit handoffs for a single task.
# 函数用途: 根据任务断连/超时状态创建 takeover run，或按 --take-over-by 把普通任务交给新负责人。
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


# LLM: _apply_takeover_run creates or reuses a replacement run and records its continuation refs.
# 函数用途: 对超时/断连任务创建 takeover run，并把新旧任务引用写进 action apply 记录。
def _apply_takeover_run(service, action, task, ctx: ActionHandlerContext):
    from .takeover_run import TakeoverRunRequest

    _mark_abandoned_attempt_before_takeover(service, action, task)
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
            created_run_ids=[result.takeover_run_id] if result.takeover_run_id else [],
        )
    )


# LLM: _apply_explicit_reassign performs the older manual handoff flow with channel probing.
# 函数用途: 用户显式指定接管人时，先确认通道可用，再写 takeover 文件和工作日志。
def _apply_explicit_reassign(service, action, task, ctx: ActionHandlerContext):
    from ..reports import ActionApplyRecord

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


# LLM: _missing_takeover_target_record returns an apply failure when explicit reassignment lacks a target.
# 函数用途: 缺少 --take-over-by 时写稳定失败记录，不改任务状态。
def _missing_takeover_target_record(service, action, task, ctx: ActionHandlerContext):
    from ..reports import ActionApplyRecord

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


# LLM: _channel_blocked_takeover_record preserves probe evidence when a handoff is unsafe.
# 函数用途: 通道仍不 OK 时拒绝接管，并把 probe 文件写入证据路径。
def _channel_blocked_takeover_record(service, action, task, ctx: ActionHandlerContext):
    from ..reports import ActionApplyRecord

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


# LLM: _needs_coordinator_handoff_action protects child subtrees from generic replacement-run takeover.
# 函数用途: 判断任务是否是已失联且仍带 child_ids 的协调节点；这种场景必须走 leadership recovery。
def _needs_coordinator_handoff_action(task) -> bool:
    if not getattr(task, "child_ids", None):
        return False
    role = str(getattr(task, "role", "") or "").lower()
    if role not in {"coordinator", "lead", "team_lead", "child_coordinator"} and "coordinator" not in role:
        return False
    status = str(getattr(task, "status", "") or "").upper()
    failure_type = str(getattr(task, "failure_type", "") or "").lower()
    return status in {"TIMEOUT", "CHANNEL_ERROR"} or failure_type in {
        "runner_timeout",
        "channel_error",
        "runner_channel_failed",
        "provider_timeout",
    }


# LLM: _should_create_takeover_run chooses replacement-run takeover for dead runner work.
# 函数用途: 超时/断通道/runner_timeout 应创建新 run 继承 refs；普通手动 reassign 仍走显式 take_over_by。
def _should_create_takeover_run(action, task) -> bool:
    triggers = {item for item in str(getattr(action, "rescue_trigger", "") or "").split(",") if item}
    if triggers & {"run_timeout", "heartbeat_stale", "status_timeout", "status_provider_timeout"}:
        return True
    if triggers & {"artifact_repair_failed", "parent_acceptance_repair_failed"}:
        return True
    status = str(getattr(task, "status", "") or "").upper()
    failure_type = str(getattr(task, "failure_type", "") or "").lower()
    return status in {"TIMEOUT", "CHANNEL_ERROR"} or failure_type in {
        "runner_timeout",
        "channel_error",
        "runner_channel_failed",
        "provider_timeout",
    }


# LLM: stale RUNNING takeover first closes the active attempt so replacement work cannot race a ghost runner.
# 函数用途: gateway/worker 进程被杀时，RUNNING 会残留 active attempt；接管前先记录 TIMEOUT 并废弃 attempt。
def _mark_abandoned_attempt_before_takeover(service, action, task) -> None:
    active_attempt_id = str(getattr(task, "runner_active_attempt_id", "") or "").strip()
    if not active_attempt_id:
        return
    triggers = {item for item in str(getattr(action, "rescue_trigger", "") or "").split(",") if item}
    if not triggers & {"run_timeout", "heartbeat_stale", "status_timeout", "status_provider_timeout"}:
        return
    from ..manager_runner_result_payload import RecordRunnerResultParams

    message = f"active runner attempt abandoned before takeover: {active_attempt_id}"
    service.manager.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            attempt_id=active_attempt_id,
            dry_run=False,
            ok=False,
            message=message,
            status="TIMEOUT",
            verification_status="UNVERIFIED",
            failure_type="runner_timeout",
        )
    )
    service.manager.abandon_runner_attempt(task.id, active_attempt_id, reason=message)


# LLM: _takeover_action_evidence keeps source and takeover refs visible in the apply record.
# 函数用途: 给 dispatch/action report 写入旧任务接管文件、新 takeover run 目录和继承 refs，便于父级继续 dispatch。
def _takeover_action_evidence(service, task, result) -> list[str]:
    refs = [task.takeover_file, task.work_log_file]
    try:
        refs.append(service.manager.load(result.takeover_run_id).task_dir)
    except FileNotFoundError:
        pass
    refs.extend(str(value) for value in result.takeover_refs.values())
    return list(dict.fromkeys(ref for ref in refs if ref))


# LLM: _takeover_apply_message preserves idempotent/exhausted takeover outcomes for the parent model.
# 函数用途: 根据接管结果生成清晰中文消息，避免失败熔断时还显示“已创建 takeover run”。
def _takeover_apply_message(result) -> str:
    if result.created and result.takeover_run_id:
        return f"已创建 takeover run {result.takeover_run_id} 接管原任务。"
    if result.takeover_run_id:
        return f"未新建 takeover run；已复用 {result.takeover_run_id}。"
    return f"未新建 takeover run：{result.message}"
