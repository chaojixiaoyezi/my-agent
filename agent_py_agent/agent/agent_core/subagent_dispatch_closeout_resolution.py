# LLM: Closeout resolution predicates decide which persisted subagent runs still block final reports.
# 模块用途: 判断 DONE/VERIFIED、takeover 覆盖和 verified sibling 覆盖，避免最终收口逻辑塞回渲染模块。

from __future__ import annotations

from ..subagents.services.hierarchy_leaf_targets import task_actual_target_tokens


# LLM: all_tasks_done_verified gates deterministic closeout on the persisted task state.
# 函数用途: 只有所有任务都已解决时才允许顶层跳过额外模型总结。
def all_tasks_done_verified(tasks: list[object]) -> bool:
    return all(task_resolved_for_closeout(task, tasks) for task in tasks)


# LLM: blocking_task_ids identifies run ids that make the whole hierarchy incomplete.
# 函数用途: 找出尚未被 DONE/VERIFIED、takeover 或 verified sibling 覆盖的节点。
def blocking_task_ids(tasks: list[object]) -> list[str]:
    blockers: list[str] = []
    for task in tasks:
        task_id = str(getattr(task, "id", "") or "")
        if task_id and not task_resolved_for_closeout(task, tasks):
            blockers.append(task_id)
    return blockers


# LLM: task_resolved_for_closeout treats superseded sources as resolved only with verified replacements.
# 函数用途: 判断单个任务是否已经完成，或是否被明确接管/修复产物覆盖。
def task_resolved_for_closeout(task: object, tasks: list[object]) -> bool:
    status = str(getattr(task, "status", "") or "").upper()
    verification = str(getattr(task, "verification_status", "") or "").upper()
    if status == "DONE" and verification == "VERIFIED":
        return True
    if _task_targets_resolved_by_verified_siblings(task, tasks):
        return True
    if status != "TAKEN_OVER":
        return False
    replacement = _task_by_id(tasks, str(getattr(task, "takeover_by", "") or "").strip())
    return bool(replacement and _task_done_verified(replacement))


# LLM: done_verified_count counts terminal or covered rows using the same closeout predicate.
# 函数用途: 给最终报告提供严格完成数，不把口头完成或待验收算成完成。
def done_verified_count(tasks: list[object]) -> int:
    return sum(1 for task in tasks if task_resolved_for_closeout(task, tasks))


# LLM: _task_targets_resolved_by_verified_siblings lets verified repair leaves cover stale failed leaves.
# 函数用途: 如果旧失败任务的具体产物已被其它 DONE/VERIFIED 任务覆盖，就不继续阻塞整棵树。
def _task_targets_resolved_by_verified_siblings(task: object, tasks: list[object]) -> bool:
    targets = task_actual_target_tokens(task)
    if not targets:
        return False
    verified_targets: set[str] = set()
    for other in tasks:
        if other is task or not _task_done_verified(other):
            continue
        verified_targets.update(task_actual_target_tokens(other))
    return bool(verified_targets and targets.issubset(verified_targets))


# LLM: _task_by_id performs exact in-memory lookup and never filesystem globs user-provided ids.
# 函数用途: 在当前 list_runs 快照中按 run_id 找接管者，避免模式匹配扫描无关任务。
def _task_by_id(tasks: list[object], run_id: str) -> object | None:
    for task in tasks:
        if str(getattr(task, "id", "") or "") == run_id:
            return task
    return None


# LLM: _task_done_verified is the strict terminal success predicate reused by closeout helpers.
# 函数用途: 精确判断任务是否 DONE/VERIFIED；不给 AWAITING_ACCEPTANCE 或口头完成放行。
def _task_done_verified(task: object) -> bool:
    return (
        str(getattr(task, "status", "") or "").upper() == "DONE"
        and str(getattr(task, "verification_status", "") or "").upper() == "VERIFIED"
    )
