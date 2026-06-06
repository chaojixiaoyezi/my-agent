
from __future__ import annotations

from dataclasses import replace


def scope_main_visible_snapshot(agent: object, snapshot: object, remembered: set[str]) -> object:
    del agent
    if not remembered:
        return snapshot
    return replace(snapshot, runs=_runs_in_remembered_scope(list(getattr(snapshot, "runs", []) or []), remembered))


def status_buckets(nodes: list[dict[str, object]]) -> dict[str, list[str]]:
    buckets = {"running": [], "blocked": [], "completed": [], "failed": [], "takeover_candidates": []}
    for node in nodes:
        _add_status_bucket(buckets, str(node.get("run_id") or ""), str(node.get("status") or "").upper())
    return buckets


def visible_nodes(nodes: list[dict[str, object]], raw_run_ids: object) -> list[dict[str, object]]:
    if not isinstance(raw_run_ids, (list, tuple, set)):
        return nodes
    visible = {str(item or "").strip() for item in raw_run_ids if str(item or "").strip()}
    if not visible:
        return nodes
    return [node for node in nodes if str(node.get("run_id") or "").strip() in visible]


def coordination_advice(nodes: list[dict[str, object]], allowed_tools: object = None) -> dict[str, object]:
    pending = _run_ids_with_status(nodes, {"CREATED", "QUEUED", "PENDING", "PLANNING", "RUNNING", "AWAITING_ACCEPTANCE"})
    completed = _run_ids_with_status(nodes, {"DONE"})
    blocked = _run_ids_with_status(nodes, {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"})
    allowed = _allowed_tool_set(allowed_tools)
    return {
        "schema_version": "agent_tree_coordination_advice.v1",
        "soft_only": True,
        "pending_child_run_ids": pending,
        "completed_child_run_ids": completed,
        "blocked_child_run_ids": blocked,
        "missing_outputs_while_running_is_failure": False,
        "should_take_over_running_children": False,
        "suggested_tool_call": _coordination_suggested_tool_call(bool(pending), allowed),
        "next_step_zh": _coordination_next_step(bool(pending), allowed),
    }


def _runs_in_remembered_scope(runs: list[object], remembered: set[str]) -> list[object]:
    selected = set(remembered)
    while _expand_once(runs, selected):
        pass
    return [row for row in runs if str(getattr(row, "run_id", "") or "") in selected]


def _expand_once(runs: list[object], selected: set[str]) -> bool:
    changed = False
    for row in runs:
        run_id = str(getattr(row, "run_id", "") or "")
        if run_id and run_id not in selected and _row_touches_selected(row, selected):
            selected.add(run_id)
            changed = True
    return changed


def _row_touches_selected(row: object, selected: set[str]) -> bool:
    parent = str(getattr(row, "parent_run_id", "") or getattr(row, "parent_id", "") or "")
    root = str(getattr(row, "root_run_id", "") or getattr(row, "root_id", "") or "")
    return parent in selected or root in selected


def _add_status_bucket(buckets: dict[str, list[str]], run_id: str, status: str) -> None:
    if not run_id:
        return
    if status == "RUNNING":
        buckets["running"].append(run_id)
    if status == "BLOCKED":
        buckets["blocked"].append(run_id)
        buckets["takeover_candidates"].append(run_id)
    if status == "DONE":
        buckets["completed"].append(run_id)
    if status in {"FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        buckets["failed"].append(run_id)
        buckets["takeover_candidates"].append(run_id)


def _run_ids_with_status(nodes: list[dict[str, object]], statuses: set[str]) -> list[str]:
    return [
        str(node.get("run_id") or "")
        for node in nodes
        if str(node.get("run_id") or "") and str(node.get("status") or "").strip().upper() in statuses
    ]


def _allowed_tool_set(raw_tools: object) -> set[str] | None:
    if raw_tools is None:
        return None
    if not isinstance(raw_tools, (list, tuple, set)):
        return set()
    return {str(tool).strip() for tool in raw_tools if str(tool).strip()}


def _tool_allowed(allowed_tools: set[str] | None, tool: str) -> bool:
    return allowed_tools is None or tool in allowed_tools


def _coordination_next_step(has_pending: bool, allowed_tools: set[str] | None = None) -> str:
    if not has_pending:
        if _tool_allowed(allowed_tools, "dispatch_subagents"):
            return "如果只是查看状态，直接向用户汇报；只有用户要推进或恢复时才调用 dispatch_subagents。"
        return "如果只是查看状态，直接向用户汇报；本轮没有调度工具时，不要声称已经推进下级代理。"
    guidance = "先调用 wait 登记非阻塞提醒，等后台提醒/完成事件后再看；期间可继续自己的工作或回复用户。"
    if _tool_allowed(allowed_tools, "send_guidance"):
        guidance += "只有要补充具体指令时，才给具体 run_id 发 send_guidance。"
    dispatch = (
        "只有下级 BLOCKED/FAILED/TIMEOUT、用户明确要求接手，或超过任务约定等待时间时，才考虑补派或接手。"
        if _tool_allowed(allowed_tools, "dispatch_subagents")
        else "本轮没有调度工具时，只做状态观察，不要安排补派或接手。"
    )
    return (
        "还有下级代理在运行、规划或等待验收时，不要把目标目录暂时为空或占位报告当失败；"
        f"{guidance}{dispatch}"
    )


def _coordination_suggested_tool_call(has_pending: bool, allowed_tools: set[str] | None) -> dict[str, object] | None:
    if not has_pending or not _tool_allowed(allowed_tools, "wait"):
        return None
    return {"tool": "wait", "seconds": 120, "reason": "等待下级代理完成或产出新进展"}


__all__ = ["coordination_advice", "scope_main_visible_snapshot", "status_buckets", "visible_nodes"]
