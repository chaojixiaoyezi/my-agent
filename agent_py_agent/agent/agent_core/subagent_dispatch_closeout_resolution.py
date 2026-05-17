# LLM: Closeout resolution predicates decide which persisted subagent runs still block final reports.
# 模块用途: 判断 DONE/VERIFIED、takeover 覆盖和 verified sibling 覆盖，避免最终收口逻辑塞回渲染模块。

from __future__ import annotations

from ..subagents.coverage_records import all_task_coverage_records, coverage_records_resolve_run
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
    if _task_explicitly_covered_by_verified_run(task, tasks):
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


# LLM: _task_targets_resolved_by_verified_siblings lets verified repair leaves cover stale terminal work.
# 函数用途: 如果旧失败或待验收任务的具体产物已被其它 DONE/VERIFIED 任务覆盖，就不继续阻塞整棵树。
def _task_targets_resolved_by_verified_siblings(task: object, tasks: list[object]) -> bool:
    if not _status_allows_sibling_target_coverage(task):
        return False
    targets = task_actual_target_tokens(task)
    if not targets:
        return False
    verified_targets: set[str] = set()
    for other in tasks:
        if other is task or not _task_done_verified(other):
            continue
        verified_targets.update(task_actual_target_tokens(other))
    return bool(verified_targets and targets.issubset(verified_targets))


# LLM: _status_allows_sibling_target_coverage prevents unstarted/running work from vanishing at closeout.
# 函数用途: 已失败/阻塞/待验收/接管的旧 run 可被同目标 verified sibling 覆盖；PLANNING/RUNNING 仍必须调度或显式取消。
def _status_allows_sibling_target_coverage(task: object) -> bool:
    status = str(getattr(task, "status", "") or "").upper()
    return status in {
        "ABANDONED",
        "AWAITING_ACCEPTANCE",
        "BLOCKED",
        "CANCELED",
        "CANCELLED",
        "ERROR",
        "FAILED",
        "TAKEN_OVER",
        "TIMEOUT",
    }


# LLM: _task_explicitly_covered_by_verified_run honors machine coverage_records, not prose fallback.
# 函数用途: 坏 run 只有被 coverage_records 指向 DONE/VERIFIED coverer 时，才不再阻塞最终收口。
def _task_explicitly_covered_by_verified_run(task: object, tasks: list[object]) -> bool:
    run_id = str(getattr(task, "id", "") or "")
    return coverage_records_resolve_run(run_id, all_task_coverage_records(tasks), lambda item: _task_id_done(tasks, item))


# LLM: _task_id_done keeps coverage closeout tied to the current task snapshot.
# 函数用途: 按 run_id 查找 coverer，并确认它已经 DONE/VERIFIED。
def _task_id_done(tasks: list[object], run_id: str) -> bool:
    task = _task_by_id(tasks, run_id)
    return bool(task and _task_done_verified(task))


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
