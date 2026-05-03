"""LLM: task planning and decomposition - pure decision functions without side effects.

给人看的解释：
提取父代理 planner 的纯决策函数，不产生任何副作用。
决策结果通过返回值传递，由调用方负责执行。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..capability_config import CapabilityConfig

if TYPE_CHECKING:
    from ..core import SimpleAgent


# ---------------------------------------------------------------------------
# Planner state collection
# ---------------------------------------------------------------------------


def build_parent_planner_state(
    agent: SimpleAgent,
    cfg: CapabilityConfig,
    *,
    max_runners: int,
    limit: int,
    reviewer: str,
    note: str,
) -> dict[str, Any]:
    """Collect state snapshot for parent planner decision making.

    This is a pure read-only operation - no state is modified.
    Returns a dict with gate summary, board summary, and per-category items.
    """
    from .runner_dispatch import _dispatch_patch_review_run_ids, _dispatch_runner_candidates, _limit_items

    tasks = agent.subagents.list_runs()
    board_limit = limit if limit > 0 else len(tasks)
    board = agent.subagents.build_board(recent_limit=board_limit)
    due_report = agent.subagents.due_check(cfg)
    action_plan = agent.subagents.plan_actions(cfg)
    runner_candidates = _dispatch_runner_candidates(tasks, max_runners)
    patch_run_ids = _limit_items(_dispatch_patch_review_run_ids(tasks), limit)
    acceptance_report = agent.subagents.review_acceptances(
        apply=False,
        reviewer=reviewer,
        note=note,
        limit=limit,
    )
    active_tasks = [
        task
        for task in tasks
        if task.status not in {"DONE", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER"}
        or task.verification_status == "NEEDS_ACCEPTANCE"
    ]
    open_requests = []
    open_gaps = []
    for task in tasks:
        for request in task.capability_requests:
            if request.status == "OPEN":
                open_requests.append({
                    "run_id": task.id,
                    "request_id": request.id,
                    "needed_capability": request.needed_capability,
                    "problem": request.problem,
                    "expected_output": request.expected_output,
                })
        for gap in task.capability_gaps:
            if gap.status == "OPEN":
                open_gaps.append({
                    "run_id": task.id,
                    "gap_id": gap.id,
                    "needed_capability": gap.needed_capability,
                    "problem": gap.problem,
                })

    gate_summary = {
        "total_tasks": len(tasks),
        "active_tasks": len(active_tasks),
        "due_issues": due_report.summary.get("total", 0),
        "action_items": action_plan.summary.get("total", 0),
        "runner_candidates": len(runner_candidates),
        "patch_reviews": len(patch_run_ids),
        "acceptance_records": len(acceptance_report.records),
        "open_capability_requests": len(open_requests),
        "open_capability_gaps": len(open_gaps),
    }
    gate_summary["needs_planner"] = int(
        any(
            gate_summary[key] > 0
            for key in (
                "active_tasks",
                "due_issues",
                "action_items",
                "runner_candidates",
                "patch_reviews",
                "acceptance_records",
                "open_capability_requests",
                "open_capability_gaps",
            )
        )
    )

    from .planner import _limit_items as limit_items_aliased
    from .runner_dispatch import _limit_items

    return {
        "gate": gate_summary,
        "board_summary": board.summary,
        "active_tasks": [_task_state_for_planner(task) for task in _limit_items(active_tasks, limit)],
        "due_issues": [
            {
                "run_id": issue.run_id,
                "severity": issue.severity,
                "kind": issue.kind,
                "message": issue.message,
                "suggested_action": issue.suggested_action,
                "status": issue.status,
                "goal": issue.goal,
                "risk_flags": issue.risk_flags,
            }
            for issue in _limit_items(due_report.issues, limit)
        ],
        "action_items": [
            {
                "run_id": item.run_id,
                "severity": item.severity,
                "priority": item.priority,
                "action": item.action,
                "reason": item.reason,
                "would_change_status_to": item.would_change_status_to,
            }
            for item in _limit_items(action_plan.actions, limit)
        ],
        "runner_candidates": [_task_state_for_planner(task) for task in runner_candidates],
        "patch_review_run_ids": patch_run_ids,
        "acceptance_records": [
            {
                "run_id": record.run_id,
                "decision": record.decision,
                "ok": record.ok,
                "message": record.message,
                "evidence_count": record.evidence_count,
                "test_count": record.test_count,
            }
            for record in _limit_items(acceptance_report.records, limit)
        ],
        "open_capability_requests": _limit_items(open_requests, limit),
        "open_capability_gaps": _limit_items(open_gaps, limit),
    }


def _task_state_for_planner(task) -> dict[str, Any]:
    """Compress task state for planner prompt."""
    return {
        "run_id": task.id,
        "status": task.status,
        "verification_status": task.verification_status,
        "channel_status": task.channel_status,
        "goal": task.goal,
        "owner": task.owner,
        "final_owner": task.final_owner,
        "updated_at": task.updated_at,
        "heartbeat_at": task.heartbeat_at,
        "evidence_count": len(task.evidence),
        "open_request_count": sum(1 for item in task.capability_requests if item.status == "OPEN"),
        "open_gap_count": sum(1 for item in task.capability_gaps if item.status == "OPEN"),
    }


def build_parent_planner_prompt(
    state: dict[str, Any],
    *,
    apply: bool,
    execute_runners: bool,
    max_runners: int,
    runner_instruction: str,
) -> str:
    """Build the parent planner LLM prompt from state snapshot.

    Returns a complete prompt string ready for LLM invocation.
    """
    import json

    payload = json.dumps(state, ensure_ascii=False, indent=2)
    mode = "apply" if apply else "dry-run"
    return (
        "# Parent Planner Tick\n\n"
        "你是父代理 planner。这个 tick 来自定时 watch，不是浅层 heartbeat。\n"
        "你必须根据状态快照判断是否有待处理事项；如果有 active/pending/stalled/"
        "needs-intervention，不允许只返回 HEARTBEAT_OK。\n\n"
        "你可以使用只读工具核对状态，但不要直接写文件。真正写回由调度器按审计流程执行。\n\n"
        "## Runtime\n\n"
        f"- mode: {mode}\n"
        f"- execute_runners: {execute_runners}\n"
        f"- cli_max_runners: {max_runners}\n"
        f"- existing_runner_instruction: {runner_instruction or 'none'}\n\n"
        "## State Snapshot\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n\n"
        "## Decision Rules\n\n"
        "- 如果 gate.needs_planner 为 0，可以返回 HEARTBEAT_OK。\n"
        "- 如果 gate.needs_planner 为 1，必须给出 DISPATCH 或 BLOCKED_REPORT。\n"
        "- 你可以建议 runner_instruction，但不能提高 cli_max_runners，只能建议更小或相等的数量。\n"
        "- 你输出的 actions 只是建议；系统会再用规则调度器验证和执行。\n\n"
        "## Required Output\n\n"
        "最后必须输出一个机器可解析结果块，格式如下。结果块里只能放裸 JSON object，"
        "不要使用 Markdown 代码围栏。\n\n"
        "[PARENT_PLANNER_RESULT]\n"
        "{\n"
        '  "decision": "DISPATCH",\n'
        '  "summary": "本轮父代理判断摘要",\n'
        '  "should_dispatch": true,\n'
        '  "runner_instruction": "给本轮 runner 的额外指令，可为空",\n'
        '  "suggested_max_runners": 1,\n'
        '  "actions": [\n'
        '    {"action": "execute_runner|review_acceptance|route_capability|takeover|report_blocker", "run_id": "", "priority": 1, "reason": ""}\n'
        "  ],\n"
        '  "blockers": [],\n'
        '  "risks": [],\n'
        '  "notes": []\n'
        "}\n"
        "[/PARENT_PLANNER_RESULT]\n"
    )


def combine_runner_instruction(base: str, planner_instruction: str) -> str:
    """Merge CLI instruction and planner instruction, CLI takes visual priority."""
    base = base.strip()
    planner_instruction = planner_instruction.strip()
    if base and planner_instruction:
        return f"{base}\n\n父代理 planner 补充指令：{planner_instruction}"
    return base or planner_instruction