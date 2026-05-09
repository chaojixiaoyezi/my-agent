# LLM: Progress payload helpers keep orchestration tool responses refs-only and compact.
# 模块用途: 给 runner-context dispatch 返回直接 child 进度摘要，避免主工具文件继续膨胀。

from __future__ import annotations

from .runner_context import current_subagent_run_id


# LLM: direct_children_progress_payload teaches parent runners to continue pending children.
# 函数用途: runner 内 dispatch 后返回直接 child 状态，避免把限速未跑完的 PLANNING 误判为失败。
def direct_children_progress_payload(agent) -> dict[str, object]:
    parent_run_id = current_subagent_run_id(agent)
    if not parent_run_id:
        return {}
    try:
        direct_children = [
            item for item in agent.subagents.list_runs()
            if str(getattr(item, "parent_id", "")) == parent_run_id
        ]
    except Exception:
        return {}
    payload = _progress_payload(parent_run_id, direct_children)
    if payload["direct_children"]["planning_run_ids"] or payload["direct_children"]["running_run_ids"]:
        payload["direct_children"]["continue_hint"] = (
            "仍有直接 child 处于 PLANNING/RUNNING；这通常是限速或串行调度造成的。"
            "继续调用 dispatch_subagents，不要把 PLANNING 直接判为失败。"
        )
    return payload


# LLM: _progress_payload folds task statuses without expanding child artifacts.
# 函数用途: 只统计直接 child 的 id/status，保持工具响应小而可恢复。
def _progress_payload(parent_run_id: str, direct_children: list) -> dict[str, object]:
    by_status: dict[str, int] = {}
    planning_ids: list[str] = []
    running_ids: list[str] = []
    for item in direct_children:
        status = str(getattr(item, "status", "") or "UNKNOWN").upper()
        by_status[status] = by_status.get(status, 0) + 1
        if status == "PLANNING":
            planning_ids.append(str(getattr(item, "id", "")))
        if status == "RUNNING":
            running_ids.append(str(getattr(item, "id", "")))
    return {
        "direct_children": {
            "parent_run_id": parent_run_id,
            "total": len(direct_children),
            "by_status": by_status,
            "planning_run_ids": [item for item in planning_ids if item],
            "running_run_ids": [item for item in running_ids if item],
        }
    }
