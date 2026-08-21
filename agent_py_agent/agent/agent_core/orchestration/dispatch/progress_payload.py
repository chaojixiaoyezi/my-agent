from __future__ import annotations

"""模型可见的直接子代理状态投影。

LLM: This module reports host-owned child status only. It must never infer quality
from model text or output files and must never ask the model to push the scheduler.

模块用途: 把当前代理的直接下级状态整理成事实回执；启动、重试和恢复留在底层运行时。
"""

from ....runtime_errors import runtime_error_report
from ....subagents.models import (
    SUBAGENT_ENDED_STATUSES,
    SUBAGENT_FAILURE_STATUSES,
    SUBAGENT_TASK_STATUSES,
    TaskStatus,
    task_status_in,
)
from ...runner.context import current_subagent_run_id


# LLM: The projection reads the canonical task store and adds advice that never mutates a run.
# 函数用途: 返回当前子代理的直接孩子状态，供工具回执和父级自然汇总使用。
def direct_children_progress_payload(agent) -> dict[str, object]:
    parent_run_id = current_subagent_run_id(agent)
    if not parent_run_id:
        return {}
    direct_children, load_error = _direct_children(agent, parent_run_id)
    if load_error:
        return {"direct_children": _children_load_error_payload(parent_run_id, load_error)}
    payload = _progress_payload(parent_run_id, direct_children)
    _attach_direct_child_next_action(payload["direct_children"])
    return payload


# LLM: Parent identity is an exact structured run id; goal text never participates in selection.
# 函数用途: 从任务账本读取当前代理的直接孩子，不展开整棵树。
def _direct_children(agent, parent_run_id: str) -> tuple[list, BaseException | None]:
    try:
        tasks = list(agent.subagents.list_runs())
        return [
            item
            for item in tasks
            if str(getattr(item, "parent_id", "") or "") == parent_run_id
        ], None
    except Exception as exc:
        return [], exc


# LLM: A failed read is explicit unknown state, never an empty-success projection.
# 函数用途: 子代理账本读失败时生成可审计错误事实。
def _children_load_error_payload(parent_run_id: str, exc: BaseException) -> dict[str, object]:
    return {
        "parent_run_id": parent_run_id,
        "total": 0,
        "by_status": {},
        "planning_run_ids": [],
        "running_run_ids": [],
        "failed_run_ids": [],
        "unfinished_run_ids": [],
        "has_unfinished": False,
        "has_failures": False,
        "all_terminal": False,
        "load_error": runtime_error_report(exc, context="direct_children.list_runs"),
        "load_error_hint": "直接子代理列表读取失败；这不代表没有子代理或已经全部完成。",
    }


# LLM: Advice mirrors 会话运行时's event-driven relation: wait for active children, review failures,
# and summarize terminal results. There is deliberately no dispatch or automatic repair action.
# 函数用途: 给事实状态附一条普通人能看懂的下一步说明，不执行任何调度。
def _attach_direct_child_next_action(children: dict[str, object]) -> None:
    if children["has_unfinished"]:
        children.update(_unfinished_child_payload(children))
        return
    if children["has_failures"]:
        children.update(_failed_children_payload(children["failed_run_ids"]))
        return
    if children.get("unknown_run_ids"):
        children.update(_unknown_status_payload(children["unknown_run_ids"]))
        return
    if children["all_terminal"]:
        children.update(_closeout_payload())


# LLM: Planning and running are both normal active states owned by the automatic scheduler.
# 函数用途: 告诉父代理哪些下级仍在工作，避免重复催跑或重复创建。
def _unfinished_child_payload(children: dict[str, object]) -> dict[str, object]:
    running = [str(item) for item in children.get("running_run_ids") or [] if str(item)]
    planning = [str(item) for item in children.get("planning_run_ids") or [] if str(item)]
    return {
        "next_action": "wait_for_direct_children",
        "wait_hint": (
            "仍有直接 child 在规划或运行；创建后由系统自动推进，完成事件会送回直接父级。"
            "可以继续自己的工作或结束本回合，不要重复催跑或重建同一任务。"
        ),
        "active_run_ids": [*running, *planning],
    }


# LLM: Failure is a host status fact; this projection does not decide whether replacement is needed.
# 函数用途: 下级明确失败时提示父代理读取原因并如实处理。
def _failed_children_payload(run_ids: list[str]) -> dict[str, object]:
    return {
        "next_action": "review_failed_direct_children",
        "failure_hint": (
            "有直接 child 已明确失败或阻塞。先查看结构化原因并如实说明；"
            "可恢复故障由底层重试，只有确实需要替代工作时才新建边界清楚的 child。"
        ),
        "failed_run_ids": [str(item) for item in run_ids if str(item)],
    }


# LLM: Unknown aliases are surfaced, not coerced into completion or failure.
# 函数用途: 遇到旧状态或未知状态时要求查看原始事实。
def _unknown_status_payload(run_ids: list[str]) -> dict[str, object]:
    return {
        "next_action": "await_host_reconciliation_for_unknown_direct_children",
        "status_review_hint": "存在未知或旧协议状态；等待宿主恢复/诊断事实，不要猜成完成或失败，也不要轮询。",
        "unknown_run_ids": [str(item) for item in run_ids if str(item)],
    }


# LLM: Terminal children hand their natural results back; no output.json or verifier is required.
# 函数用途: 所有下级结束后提示父代理自然整合和回复。
def _closeout_payload() -> dict[str, object]:
    return {
        "next_action": "summarize_direct_children",
        "closeout_hint": "直接 child 已全部结束；请根据其自然语言结果和必要 refs 完成自己的工作并正常回复父级。",
    }


# LLM: Buckets derive only from canonical status enum membership.
# 函数用途: 把直接孩子按规划、运行、失败、完成和未知状态分组。
def _progress_payload(parent_run_id: str, direct_children: list) -> dict[str, object]:
    by_status: dict[str, int] = {}
    buckets: dict[str, list[str]] = {
        "planning": [],
        "running": [],
        "failed": [],
        "completed": [],
        "unknown": [],
    }
    for item in direct_children:
        status = str(getattr(item, "status", "") or "UNKNOWN")
        run_id = str(getattr(item, "id", "") or "")
        by_status[status] = by_status.get(status, 0) + 1
        bucket = _status_bucket(status)
        if bucket:
            buckets[bucket].append(run_id)
    planning_ids = buckets["planning"]
    running_ids = buckets["running"]
    failed_ids = buckets["failed"]
    completed_ids = buckets["completed"]
    unknown_ids = buckets["unknown"]
    unfinished_ids = [item for item in [*planning_ids, *running_ids] if item]
    terminal_ids = [item for item in [*completed_ids, *failed_ids] if item]
    return {
        "direct_children": {
            "parent_run_id": parent_run_id,
            "total": len(direct_children),
            "by_status": by_status,
            "planning_run_ids": [item for item in planning_ids if item],
            "running_run_ids": [item for item in running_ids if item],
            "failed_run_ids": [item for item in failed_ids if item],
            "completed_run_ids": [item for item in completed_ids if item],
            "unknown_run_ids": [item for item in unknown_ids if item],
            "unfinished_run_ids": unfinished_ids,
            "terminal_run_ids": terminal_ids,
            "has_unfinished": bool(unfinished_ids),
            "has_failures": bool(failed_ids),
            "all_terminal": bool(direct_children) and len(terminal_ids) == len(direct_children),
        }
    }


# LLM: Classification is enum-based and intentionally independent of goal/result text.
# 函数用途: 把一个权威状态映射到展示分组，未知旧状态单独暴露。
def _status_bucket(status: str) -> str:
    if task_status_in(status, {TaskStatus.PLANNING.value, TaskStatus.PENDING.value}):
        return "planning"
    if task_status_in(status, {TaskStatus.RUNNING.value}):
        return "running"
    if task_status_in(status, SUBAGENT_FAILURE_STATUSES):
        return "failed"
    if task_status_in(status, SUBAGENT_ENDED_STATUSES):
        return "completed"
    if not task_status_in(status, SUBAGENT_TASK_STATUSES):
        return "unknown"
    return ""


__all__ = ["direct_children_progress_payload"]
