
from __future__ import annotations

from dataclasses import replace

from ...subagents.models import SUBAGENT_FAILED_RESULT_STATUSES, TaskStatus, task_status_in


def scope_main_visible_snapshot(agent: object, snapshot: object, remembered: set[str]) -> object:
    del agent
    if not remembered:
        return snapshot
    return replace(snapshot, runs=_runs_in_remembered_scope(list(getattr(snapshot, "runs", []) or []), remembered))


def status_buckets(nodes: list[dict[str, object]]) -> dict[str, list[str]]:
    buckets = {
        "running": [],
        "blocked": [],
        "completed": [],
        "failed": [],
        "takeover_candidates": [],
        "resume_candidates": [],
    }
    for node in nodes:
        _add_status_bucket(buckets, str(node.get("run_id") or ""), str(node.get("status") or "").upper())
        eligibility = node.get("resume_eligibility")
        if (
            isinstance(eligibility, dict)
            and eligibility.get("eligible") is True
            and str(node.get("run_id") or "")
        ):
            buckets["resume_candidates"].append(str(node["run_id"]))
    return buckets


def visible_nodes(nodes: list[dict[str, object]], raw_run_ids: object) -> list[dict[str, object]]:
    if not isinstance(raw_run_ids, (list, tuple, set)):
        return nodes
    visible = {str(item or "").strip() for item in raw_run_ids if str(item or "").strip()}
    if not visible:
        return nodes
    return [node for node in nodes if str(node.get("run_id") or "").strip() in visible]


def coordination_advice(nodes: list[dict[str, object]], allowed_tools: object = None) -> dict[str, object]:
    pending = _run_ids_with_status(nodes, {TaskStatus.PENDING.value, TaskStatus.PLANNING.value, TaskStatus.RUNNING.value})
    completed = _run_ids_with_status(nodes, {TaskStatus.DONE.value})
    blocked = _run_ids_with_status(nodes, {TaskStatus.BLOCKED.value} | SUBAGENT_FAILED_RESULT_STATUSES)
    allowed = _allowed_tool_set(allowed_tools)
    return {
        "schema_version": "agent_tree_coordination_advice.v1",
        "soft_only": True,
        "pending_child_run_ids": pending,
        "completed_child_run_ids": completed,
        "blocked_child_run_ids": blocked,
        "missing_outputs_while_running_is_failure": False,
        "should_take_over_running_children": False,
        "suggested_tool_call": None,
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
    if task_status_in(status, {TaskStatus.RUNNING.value}):
        buckets["running"].append(run_id)
    if task_status_in(status, {TaskStatus.BLOCKED.value}):
        buckets["blocked"].append(run_id)
        buckets["takeover_candidates"].append(run_id)
    if task_status_in(status, {TaskStatus.DONE.value}):
        buckets["completed"].append(run_id)
    if task_status_in(status, SUBAGENT_FAILED_RESULT_STATUSES):
        buckets["failed"].append(run_id)
        buckets["takeover_candidates"].append(run_id)


def _run_ids_with_status(nodes: list[dict[str, object]], statuses: set[str]) -> list[str]:
    return [
        str(node.get("run_id") or "")
        for node in nodes
        if str(node.get("run_id") or "") and task_status_in(node.get("status"), statuses)
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
    guidance = "先结束本回合，派工监督提醒/完成事件会自动唤醒；期间可继续自己的工作或回复用户。"
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


__all__ = ["coordination_advice", "scope_main_visible_snapshot", "status_buckets", "visible_nodes"]
