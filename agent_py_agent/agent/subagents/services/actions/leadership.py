
from __future__ import annotations

"""coordinator leadership recovery handlers for subagent action apply."""

from dataclasses import dataclass

from .handlers import (
    ActionHandlerContext,
    CoordinatorHandoffErrorRequest,
    RecordAfterTaskActionParams,
    coordinator_handoff_error_record,
)


@dataclass(frozen=True)
class RecoveredLeaderSaveRequest:
    service: object
    leader: object
    task: object
    original_child_ids: list[str]
    updated_children: list[str]
    now: float


def apply_recover_coordinator_leadership(service, action, task, ctx: ActionHandlerContext):
    leader_id = ctx.take_over_by
    if not leader_id:
        return coordinator_handoff_error_record(
            CoordinatorHandoffErrorRequest(
                service,
                action,
                task,
                ctx,
                "recover_coordinator_leadership 需要 --take-over-by 指向新 leader run_id。",
            )
        )
    try:
        leader = service.manager.load(leader_id)
    except FileNotFoundError:
        return coordinator_handoff_error_record(
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
    _save_recovered_leader(
        RecoveredLeaderSaveRequest(service, leader, task, original_child_ids, updated_children, ctx.now)
    )
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


def _save_recovered_leader(request: RecoveredLeaderSaveRequest) -> None:
    service = request.service
    leader = request.leader
    task = request.task
    now = request.now
    leader.child_ids = _unique_refs([*leader.child_ids, *request.updated_children])
    leader.updated_at = now
    service.manager.save(leader)
    task.child_ids = [leader.id] if leader.id in request.original_child_ids or leader.parent_id == task.id else []
    task.updated_at = now
    service.manager.save_hierarchy_links(task)


def _rehang_child_subtrees(service, child_ids: list[str], leader, now: float) -> list[str]:
    updated_ids: list[str] = []
    for child_id in child_ids:
        if child_id == leader.id:
            continue
        try:
            child = service.manager.load(child_id)
        except FileNotFoundError:
            continue
        _move_child_under_leader(service, child, leader, now)
        updated_ids.append(child.id)
    return updated_ids


def _move_child_under_leader(service, child, leader, now: float) -> None:
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


def _task_dirs(service, run_ids: list[str]) -> list[str]:
    refs: list[str] = []
    for run_id in run_ids:
        try:
            refs.append(service.manager.load(run_id).task_dir)
        except FileNotFoundError:
            continue
    return refs


def _unique_refs(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))
