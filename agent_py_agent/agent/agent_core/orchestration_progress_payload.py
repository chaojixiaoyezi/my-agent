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
    if payload["direct_children"]["needs_more_dispatch"]:
        payload["direct_children"].update({
            "next_action": "continue_dispatch_direct_children",
            "suggested_tool_call": {
                "tool": "dispatch_subagents",
                "apply": True,
                "execute_runners": True,
                "run_ids": payload["direct_children"]["unfinished_run_ids"],
                "workflow_mode": "auto",
            },
            "continue_hint": (
                "仍有直接 child 处于 PLANNING/RUNNING；这通常是限速或串行调度造成的。"
                "继续调用 dispatch_subagents，不要把 PLANNING 直接判为失败。"
            ),
        })
    elif payload["direct_children"]["needs_recovery"]:
        payload["direct_children"].update({
            "next_action": "inspect_or_rescue_direct_children",
            "suggested_tool_call": {
                "tool": "dispatch_subagents",
                "apply": True,
                "execute_runners": True,
                "run_ids": payload["direct_children"]["recovery_run_ids"],
                "workflow_mode": "auto",
            },
            "suggested_recovery_child_tool_call": _recovery_child_tool_call(
                payload["direct_children"]["recovery_run_ids"]
            ),
            "recovery_hint": (
                "有直接 child 已 BLOCKED/FAILED/TIMEOUT；先用这些 run_ids 尝试受控重试。"
                "如果仍不可重试，按 suggested_recovery_child_tool_call 创建恢复 child。"
                "恢复 child 默认可以是 worker；只有确实需要继续拆多层时，父节点才改成 coordinator。"
            ),
        })
    return payload


# LLM: _progress_payload folds task statuses without expanding child artifacts.
# 函数用途: 只统计直接 child 的 id/status，保持工具响应小而可恢复。
def _progress_payload(parent_run_id: str, direct_children: list) -> dict[str, object]:
    by_status: dict[str, int] = {}
    planning_ids: list[str] = []
    running_ids: list[str] = []
    recovery_ids: list[str] = []
    for item in direct_children:
        status = str(getattr(item, "status", "") or "UNKNOWN").upper()
        by_status[status] = by_status.get(status, 0) + 1
        if status == "PLANNING":
            planning_ids.append(str(getattr(item, "id", "")))
        if status == "RUNNING":
            running_ids.append(str(getattr(item, "id", "")))
        if status in {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
            recovery_ids.append(str(getattr(item, "id", "")))
    unfinished_ids = [item for item in [*planning_ids, *running_ids] if item]
    recovery_ids = [item for item in recovery_ids if item]
    return {
        "direct_children": {
            "parent_run_id": parent_run_id,
            "total": len(direct_children),
            "by_status": by_status,
            "planning_run_ids": [item for item in planning_ids if item],
            "running_run_ids": [item for item in running_ids if item],
            "recovery_run_ids": recovery_ids,
            "unfinished_run_ids": unfinished_ids,
            "needs_more_dispatch": bool(unfinished_ids),
            "needs_recovery": bool(recovery_ids),
        }
    }


# LLM: _recovery_child_tool_call suggests a flexible child recovery step without forcing a coordinator.
# 函数用途: 生成 refs-only 恢复 child 创建建议；真实创建仍必须由父 runner 自己调用 schedule_child_subagents。
def _recovery_child_tool_call(recovery_run_ids: list[str]) -> dict[str, object]:
    ids = [item for item in recovery_run_ids if item]
    joined_ids = ", ".join(ids)
    return {
        "tool": "schedule_child_subagents",
        "apply": True,
        "role_selection_hint": "默认用 worker；只有恢复本身需要继续拆下级任务时，父节点才把 role 改成 coordinator/lead。",
        "children": [
            {
                "role": "worker",
                "agent_name": "recovery-worker",
                "goal": (
                    "接管或修复这些直接 child runs："
                    f"{joined_ids}。先读取它们的 status/failure_handoff/takeover refs，"
                    "不要改写健康分支；如果只是单点修复就直接完成，"
                    "如果确实需要继续拆多层，再由父节点改派 coordinator。"
                ),
                "allowed_tools": [
                    "subagent_board",
                    "read_file",
                    "list_files",
                ],
            }
        ],
    }
