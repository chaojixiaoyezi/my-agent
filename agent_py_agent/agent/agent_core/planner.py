from __future__ import annotations

"""LLM: builds parent-planner state snapshots, prompts, and compact task projections.

给人看的解释：
父代理 planner 需要一份压缩过的现场快照，而不是把所有工单原文塞给模型。
这个文件负责收集要点、构建 planner prompt，并合并 planner 给 runner 的补充指令。
"""

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..capability_config import CapabilityConfig
from ..subagent import SubAgentTask
from ..subagents.models import SubAgentDueCheckOptions
from .runner_dispatch import (
    _dispatch_patch_review_run_ids,
    _dispatch_runner_candidates,
    _limit_items,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent

PARENT_PLANNER_READ_TOOLS = ["list_files", "read_file", "search_text"]


@dataclass(frozen=True)
class BuildGateSummaryParams:

    tasks: list
    active_tasks: list
    due_report: Any
    action_plan: Any
    runner_candidates: list
    patch_run_ids: list
    acceptance_report: Any
    open_requests: list
    open_gaps: list


def _collect_open_requests_and_gaps(
    tasks: list[SubAgentTask],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    open_requests = []
    open_gaps = []
    for task in tasks:
        open_requests.extend(_open_request_items(task))
        open_gaps.extend(_open_gap_items(task))
    return open_requests, open_gaps


def _open_request_items(task: SubAgentTask) -> list[dict[str, object]]:
    return [
        item
        for request in task.capability_requests
        if (item := _open_request_item(task, request)) is not None
    ]


def _open_gap_items(task: SubAgentTask) -> list[dict[str, object]]:
    return [
        item
        for gap in task.capability_gaps
        if (item := _open_gap_item(task, gap)) is not None
    ]


def _open_request_item(task: SubAgentTask, request) -> dict[str, object] | None:
    if request.status != "OPEN":
        return None
    return {
        "run_id": task.id,
        "request_id": request.id,
        "needed_capability": request.needed_capability,
        "problem": request.problem,
        "expected_output": request.expected_output,
    }


def _open_gap_item(task: SubAgentTask, gap) -> dict[str, object] | None:
    if gap.status != "OPEN":
        return None
    return {
        "run_id": task.id,
        "gap_id": gap.id,
        "needed_capability": gap.needed_capability,
        "problem": gap.problem,
    }


def _build_parent_planner_state(
    agent: SimpleAgent,
    cfg: CapabilityConfig,
    *,
    max_runners: int,
    limit: int,
    reviewer: str,
    note: str,
) -> dict[str, object]:

    tasks = agent.subagents.list_runs()
    board_limit = limit if limit > 0 else len(tasks)
    board = agent.subagents.build_board(recent_limit=board_limit)
    due_report = agent.subagents.due_check(params=SubAgentDueCheckOptions(config=cfg))
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
    open_requests, open_gaps = _collect_open_requests_and_gaps(tasks)

    gate_summary = _build_planner_gate_summary(
        BuildGateSummaryParams(
            tasks=tasks,
            active_tasks=active_tasks,
            due_report=due_report,
            action_plan=action_plan,
            runner_candidates=runner_candidates,
            patch_run_ids=patch_run_ids,
            acceptance_report=acceptance_report,
            open_requests=open_requests,
            open_gaps=open_gaps,
        )
    )
    return {
        "gate": gate_summary,
        "board_summary": board.summary,
        "active_tasks": _planner_task_states(active_tasks, limit),
        "due_issues": _planner_due_issues(due_report.issues, limit),
        "action_items": _planner_action_items(action_plan.actions, limit),
        "runner_candidates": [_task_state_for_planner(task) for task in runner_candidates],
        "patch_review_run_ids": patch_run_ids,
        "acceptance_records": _planner_acceptance_records(acceptance_report.records, limit),
        "open_capability_requests": _limit_items(open_requests, limit),
        "open_capability_gaps": _limit_items(open_gaps, limit),
    }


def _build_planner_gate_summary(params: BuildGateSummaryParams) -> dict[str, int]:
    return _build_gate_summary(params)


def _planner_task_states(tasks: list[SubAgentTask], limit: int) -> list[dict[str, object]]:
    return [_task_state_for_planner(task) for task in _limit_items(tasks, limit)]


def _planner_due_issues(issues, limit: int) -> list[dict[str, object]]:
    return [
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
        for issue in _limit_items(issues, limit)
    ]


def _planner_action_items(actions, limit: int) -> list[dict[str, object]]:
    return [
        {
            "run_id": item.run_id,
            "severity": item.severity,
            "priority": item.priority,
            "action": item.action,
            "reason": item.reason,
            "would_change_status_to": item.would_change_status_to,
        }
        for item in _limit_items(actions, limit)
    ]


def _planner_acceptance_records(records, limit: int) -> list[dict[str, object]]:
    return [
        {
            "run_id": record.run_id,
            "decision": record.decision,
            "ok": record.ok,
            "message": record.message,
            "evidence_count": record.evidence_count,
            "test_count": record.test_count,
        }
        for record in _limit_items(records, limit)
    ]


def _build_gate_summary(params: BuildGateSummaryParams) -> dict[str, int]:
    gate_summary = {
        "total_tasks": len(params.tasks),
        "active_tasks": len(params.active_tasks),
        "due_issues": params.due_report.summary.get("total", 0),
        "action_items": params.action_plan.summary.get("total", 0),
        "runner_candidates": len(params.runner_candidates),
        "patch_reviews": len(params.patch_run_ids),
        "acceptance_records": len(params.acceptance_report.records),
        "open_capability_requests": len(params.open_requests),
        "open_capability_gaps": len(params.open_gaps),
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
    return gate_summary


def _build_parent_planner_prompt(
    state: dict[str, object],
    *,
    apply: bool,
    execute_runners: bool,
    max_runners: int,
    runner_instruction: str,
) -> str:

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


def _task_state_for_planner(task: SubAgentTask) -> dict[str, object]:

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


def _combine_runner_instruction(base: str, planner_instruction: str) -> str:

    base = base.strip()
    planner_instruction = planner_instruction.strip()
    if base and planner_instruction:
        return f"{base}\n\n父代理 planner 补充指令：{planner_instruction}"
    return base or planner_instruction
