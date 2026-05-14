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
from .takeover_readiness import takeover_readiness_ref_order


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


# LLM: CoordinatorHandoffErrorRequest bundles failed handoff record inputs to keep helper signatures stable.
# 类用途: 保存 coordinator 领导权恢复失败记录所需上下文，避免 helper 继续散传参数。
@dataclass(frozen=True)
class CoordinatorHandoffErrorRequest:
    service: object
    action: object
    task: object
    ctx: ActionHandlerContext
    message: str


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
    from .takeover_run import TakeoverRunRequest

    if _needs_coordinator_handoff_action(task):
        return _coordinator_handoff_error_record(
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
        result = service.manager.create_takeover_run(
            TakeoverRunRequest(source_run_id=action.run_id, reason=action.reason)
        )
        task = service.manager.load(action.run_id)
        message = _takeover_apply_message(result)
        service._append_task_work_log(
            task,
            f"action_apply takeover_or_reassign: {message}",
        )
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
    }


# LLM: _should_create_takeover_run chooses replacement-run takeover for dead runner work.
# 函数用途: 超时/断通道/runner_timeout 应创建新 run 继承 refs；普通手动 reassign 仍走显式 take_over_by。
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


# LLM: apply_recover_coordinator_leadership performs explicit child subtree reparenting for stale coordinators.
# 函数用途: 把失联 coordinator 标记为被接管，并把其子任务重挂到新的 leader run 名下。
def apply_recover_coordinator_leadership(service, action, task, ctx: ActionHandlerContext):
    leader_id = ctx.take_over_by
    if not leader_id:
        return _coordinator_handoff_error_record(
            CoordinatorHandoffErrorRequest(
                service, action, task, ctx,
                "recover_coordinator_leadership 需要 --take-over-by 指向新 leader run_id。",
            )
        )
    try:
        leader = service.manager.load(leader_id)
    except FileNotFoundError:
        return _coordinator_handoff_error_record(
            CoordinatorHandoffErrorRequest(service, action, task, ctx, f"新 leader run 不存在: {leader_id}")
        )
    original_child_ids = list(task.child_ids)
    service.manager.record_takeover(
        action.run_id,
        take_over_by=leader.id,
        reason=action.reason,
        locked_files=ctx.locked_files or [],
    )
    task = service.manager.load(action.run_id)
    leader = service.manager.load(leader.id)
    updated_children = _rehang_child_subtrees(service, original_child_ids, leader, ctx.now)
    leader.child_ids = _unique_refs([*leader.child_ids, *updated_children])
    leader.updated_at = ctx.now
    service.manager.save(leader)
    task.child_ids = [leader.id] if leader.id in original_child_ids or leader.parent_id == task.id else []
    task.updated_at = ctx.now
    service.manager.save_hierarchy_links(task)
    service._append_task_work_log(
        task,
        f"action_apply recover_coordinator_leadership: 已将 {len(updated_children)} 个子任务交给 {leader.id}。",
    )
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action,
            task,
            ctx.before_status,
            ctx.before_channel_status,
            f"已把 coordinator 标记为接管，并将 {len(updated_children)} 个子任务交给 {leader.id}。",
            evidence_paths=[task.takeover_file, task.work_log_file, *(_task_dirs(service, updated_children))],
        )
    )


# LLM: apply_stop_no_progress_and_escalate records a terminal retry fuse without changing ownership or spawning work.
# 函数用途: 连续恢复无进展时写入明确 blocker/worklog，让父级停止自动重试并根据 refs 人工决策。
def apply_stop_no_progress_and_escalate(service, action, task, ctx: ActionHandlerContext):
    task.failure_type = "no_progress_fuse"
    blocker = "no_progress_fuse: 连续恢复没有进展，已停止自动重试和扩容。"
    if blocker not in task.blockers:
        task.blockers.append(blocker)
    task.current_step = "no-progress fuse tripped; waiting for parent/user decision"
    task.latest_summary = "连续恢复无进展；请父级/用户根据 checkpoint、summary 和 task-local refs 决定下一步。"
    task.updated_at = ctx.now
    service.manager.save(task)
    service._append_task_work_log(
        task,
        "action_apply stop_no_progress_and_escalate: 已触发 no-progress fuse，停止自动重试和扩容。",
    )
    return service._record_after_task_action(
        RecordAfterTaskActionParams(
            action,
            task,
            ctx.before_status,
            ctx.before_channel_status,
            "no-progress fuse 已触发：已停止自动重试和扩容，等待父级/用户决策。",
            evidence_paths=[task.work_log_file, task.task_dir],
        )
    )


# LLM: _coordinator_handoff_error_record returns a stable failed apply record without mutating task state.
# 函数用途: 构造 coordinator 领导权恢复失败记录，保持缺 leader / leader 不存在时的审计格式一致。
def _coordinator_handoff_error_record(request: CoordinatorHandoffErrorRequest):
    from ..reports import ActionApplyRecord

    service = request.service
    action = request.action
    task = request.task
    ctx = request.ctx
    return ActionApplyRecord(
        id=service.manager._new_id("apply"), action_id=action.id, run_id=action.run_id, action=action.action,
        dry_run=False, applied=False, ok=False, message=request.message,
        before_status=ctx.before_status, after_status=ctx.before_status,
        before_channel_status=ctx.before_channel_status, after_channel_status=ctx.before_channel_status,
        evidence_paths=[task.task_dir], created_at=ctx.now,
    )


# LLM: _rehang_child_subtrees moves old coordinator children below the new leader and refreshes descendant depths.
# 函数用途: 将旧 coordinator 的直接子任务重挂到 leader，并递推修正孙级深度。
def _rehang_child_subtrees(service, child_ids: list[str], leader, now: float) -> list[str]:
    updated_ids: list[str] = []
    for child_id in child_ids:
        if child_id == leader.id:
            continue
        try:
            child = service.manager.load(child_id)
        except FileNotFoundError:
            continue
        child.parent_id = leader.id
        child.supervisor = leader.id
        child.final_owner = leader.id
        child.depth = max(0, int(leader.depth or 0) + 1)
        child.updated_at = now
        service.manager.save(child)
        _refresh_descendant_depths(service, child, now)
        service._append_task_work_log(
            child,
            f"action_apply recover_coordinator_leadership: parent/supervisor/final_owner -> {leader.id}。",
        )
        updated_ids.append(child.id)
    return updated_ids


# LLM: _refresh_descendant_depths keeps grandchildren depth values consistent after a parent is rehomed.
# 函数用途: 父任务深度变化后，递推刷新所有后代的 depth 字段，但不改变它们的 parent_id。
def _refresh_descendant_depths(service, parent, now: float) -> None:
    for child_id in parent.child_ids:
        try:
            child = service.manager.load(child_id)
        except FileNotFoundError:
            continue
        child.depth = max(0, int(parent.depth or 0) + 1)
        child.updated_at = now
        service.manager.save(child)
        _refresh_descendant_depths(service, child, now)


# LLM: _task_dirs resolves updated run IDs to task directories for action evidence refs.
# 函数用途: 将已更新的 run_id 列表转换为报告里的证据路径，缺失任务会被跳过。
def _task_dirs(service, run_ids: list[str]) -> list[str]:
    refs: list[str] = []
    for run_id in run_ids:
        try:
            refs.append(service.manager.load(run_id).task_dir)
        except FileNotFoundError:
            continue
    return refs


# LLM: _unique_refs preserves ref order while removing blanks and duplicates.
# 函数用途: 合并 child_ids 时去重并保持原始顺序，避免重挂后重复子节点。
def _unique_refs(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))


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
