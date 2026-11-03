# LLM: 收口只消费宿主确认的父级和精确申请身份；live/recovery共用纯reducer，不能恢复执行或扩大授权。
# 模块用途: 关闭父回合已经结束的授权等待，并从canonical状态核对确认。
from __future__ import annotations

import time

from ..common.request_execution_ports import gateway_short_transaction
from .coordination import subagent_creation_guard
from .model_capabilities import capability_request_counts_as_open
from .models import FailureType, TaskStatus
from .services.persistence.service import (
    existing_subagent_selection_matches,
    existing_subagent_selection_metadata,
    load_subagent_task,
    mutate_existing_subagent_metadata,
)


# LLM: 原请求短门先于creation/canonical锁；旧caller省略request_ids仍即时筛选，不改变公开返回结构。
# 函数用途: 收口指定父级下的等待行，可只消费事先冻结的申请编号。
def close_stale_capability_requests(store, *, parent_task_id: str, reason: str,
                                    replaced_by_run_id: str = "", run_ids: tuple[str, ...] | None = None,
                                    request_ids: dict[str, tuple[str, ...]] | None = None,
                                    selection_metadata=None) -> list[str]:
    _validate_closeout(parent_task_id, reason)
    changed: list[str] = []
    with gateway_short_transaction("release"), store.creation_guard():
        replacement = store.load(replaced_by_run_id) if replaced_by_run_id else None
        if run_ids is None:
            report = store.list_runs_for_root_report(parent_task_id)
            if report.load_errors:
                raise OSError("capability closeout candidates unavailable")
            candidates = tuple(task.id for task in report.runs if task.parent_id == parent_task_id)
        else:
            candidates = tuple(dict.fromkeys(run_ids))
        for run_id in candidates:
            _close_waiting_run(store, run_id, parent_task_id, reason, replacement, changed,
                               request_ids=None if request_ids is None else request_ids.get(run_id, ()),
                               selection=None if selection_metadata is None else selection_metadata[run_id])
    return changed


def _validate_closeout(parent_task_id, reason):
    if reason not in {"parent_turn_ended", "parent_turn_interrupted", "replaced", "user_denied"}:
        raise ValueError("invalid capability closeout reason")
    if not parent_task_id:
        raise ValueError("parent task identity is required")


# LLM: 无IO、时间从caller传入；只改已让出的BLOCKED申请，精确集合外的新申请、grant、终态和活attempt保持原样。
# 函数用途: 在canonical锁内单调关闭选中的申请，不让旧收尾取消更新的工作。
def reduce_capability_request_closeout(current, parent_task_id, reason, replacement, request_ids, now):
    _validate_closeout(parent_task_id, reason)
    if (current.parent_id != parent_task_id or current.status != TaskStatus.BLOCKED.value
            or current.failure_type != FailureType.CAPABILITY_REQUEST.value or current.runner_active_attempt_id):
        return False
    if reason == "replaced" and (replacement is None or not (
            replacement.id != current.id and replacement.parent_id == current.parent_id
            and replacement.root_id == current.root_id and replacement.role == current.role
            and replacement.goal == current.goal and replacement.created_at > current.created_at)):
        return False
    selected = set(request_ids) if request_ids is not None else {
        request.id for request in current.capability_requests if capability_request_counts_as_open(request.status)}
    pending = [request for request in current.capability_requests
               if request.id in selected and capability_request_counts_as_open(request.status)]
    if not pending:
        return False
    resolution = {"reason": reason, "replaced_by_run_id": replacement.id if reason == "replaced" else ""}
    for request in pending:
        request.status = "CLOSED"
        request.constraints = {**dict(request.constraints or {}), "resolution_reason": reason,
                               "replaced_by_run_id": resolution["replaced_by_run_id"]}
    current.attributes = {**dict(current.attributes or {}), "capability_request_closeout": {
        "resolution": resolution, "request_ids": [request.id for request in pending],
    }}
    # 新申请和本次集合中抢先获批的申请仍有工作；历史grant不能永久保留本次BLOCKED。
    if not any(capability_request_counts_as_open(request.status) or (request.id in selected and request.status == "GRANTED")
               for request in current.capability_requests):
        current.status = TaskStatus.CANCELLED.value
        current.failure_type = FailureType.CANCELLED.value
        current.blockers = []
        current.current_tool = ""
        current.current_step = "已被替代" if reason == "replaced" else "已拒绝" if reason == "user_denied" else "已过期"
        current.ended_at = now
    current.updated_at = now
    return True


# LLM: 只依赖最新canonical对象；缺申请或身份不符都是未确认，不以没有异常充当ack。
# 函数用途: 核对冻结的申请已关闭，或已由真实grant/执行/终态接管。
def capability_request_closeout_confirmed(current, parent_task_id, request_ids):
    if current.parent_id != parent_task_id:
        return False
    requests = {request.id: request for request in current.capability_requests}
    if not request_ids or any(request_id not in requests for request_id in request_ids):
        return False
    if current.runner_active_attempt_id or current.status != TaskStatus.BLOCKED.value or current.failure_type != FailureType.CAPABILITY_REQUEST.value:
        return True
    return all(not capability_request_counts_as_open(requests[request_id].status) for request_id in request_ids)


# LLM: 写失败不在reducer内宣告changed；可选原边界在窄writer锁内复核，caller仍必须读回确认。
# 函数用途: 保持原changed列表合同并复用窄持久化叶。
def _close_waiting_run(store, run_id, parent_task_id, reason, replacement, changed, *, request_ids=None, selection=None):
    applied = []

    def close(current):
        if reduce_capability_request_closeout(current, parent_task_id, reason, replacement, request_ids, time.time()):
            applied.append(current.id)

    mutate_existing_subagent_metadata(store.workspace, run_id, close, selection=selection)
    changed.extend(applied)


# LLM: workspace由宿主原路径校验给出；只消费同一证明的精确集合及边界，不扫描历史或建manager。
# 函数用途: 在恢复窗口按精确孩子和申请集合补交同一个收口动作。
def close_existing_capability_requests(workspace, parent_task_id, reason, request_ids, *, selection_metadata=None):
    _validate_closeout(parent_task_id, reason)
    with gateway_short_transaction("release"), subagent_creation_guard(workspace):
        for run_id, selected in request_ids.items():
            mutate_existing_subagent_metadata(workspace, run_id, lambda task: reduce_capability_request_closeout(
                task, parent_task_id, reason, None, selected, time.time()),
                selection=None if selection_metadata is None else selection_metadata[run_id])


# LLM: canonical读回还须复核原边界；缺失/换身份继续未决，不把旧展示或写函数返回当成功。
# 函数用途: 精确核对整个冻结集合的收口确认。
def existing_capability_closeout_confirmed(workspace, parent_task_id, request_ids, *, selection_metadata=None):
    for run_id, selected in request_ids.items():
        task = load_subagent_task(workspace, run_id)
        if (selection_metadata is not None and not existing_subagent_selection_matches(task, selection_metadata[run_id])):
            return False
        if not capability_request_closeout_confirmed(task, parent_task_id, selected):
            return False
    return True


# LLM: 只接收原live窗口已读canonical任务；固定原owner/父级、版本和前缀，位置只由当时OPEN状态产生，不写孩子。
# 函数用途: 给首份证明保存可跨进程复核的申请库存边界，而非以后重新全量挑选。
def capability_request_selection_boundary(tasks, parent_task_id, owner_id):
    children = {}
    for task in tasks:
        if (task.parent_id != parent_task_id or task.owner != owner_id or task.status != "BLOCKED"
                or task.failure_type != "capability_request" or task.runner_active_attempt_id):
            continue
        indices = [i for i, request in enumerate(task.capability_requests)
                   if capability_request_counts_as_open(request.status)]
        if indices:
            children[task.id] = dict(existing_subagent_selection_metadata(task),
                                     state_revision=task.state_revision, open_indices=indices)
    return children


# LLM: 恢复只按证明列出的既有run逐个读canonical；原前缀/身份或版本不可证就失败，绝不扫描现在的OPEN集合。
# 函数用途: 从原库存位置还原精确申请编号，让晚到孩子和追加申请保持原样。
def existing_capability_request_snapshot(workspace, children):
    selected = {}
    for run_id, metadata in children.items():
        task = load_subagent_task(workspace, run_id)
        if not existing_subagent_selection_matches(task, metadata):
            raise ValueError("original capability selection unavailable")
        selected[run_id] = tuple(task.capability_requests[index].id for index in metadata["open_indices"])
    return selected
