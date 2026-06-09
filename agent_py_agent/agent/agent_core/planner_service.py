

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_py_agent.agent.capability.config import CapabilityConfig

from ..subagents.models import SubAgentBoardOptions, SubAgentDueCheckOptions
from .orchestration.dispatch.params import DispatchExecutionPlan

if TYPE_CHECKING:
    from ..core import SimpleAgent

PARENT_PLANNER_READ_TOOLS = ["list_files", "read_file", "search_text"]
PARENT_PLANNER_SYSTEM_PROMPT = (
    "你是 my-agent 的父级调度 planner，只负责读取调度状态并返回结构化调度决定。\n"
    "你不是普通 root 对话代理，也不是 worker/subagent runner；不要继承或模仿 runner 输出协议。\n"
    "你不能调用工具；调度器提供的 State Snapshot 是唯一事实来源。\n"
    "必须只按用户任务里的 Parent Planner Contract 输出 [PARENT_PLANNER_RESULT] JSON 结果块。\n"
    "禁止输出 [SUBAGENT_RESULT]、工具调用承诺、Markdown 代码围栏或额外解释。"
)
PARENT_PLANNER_RESULT_TEMPLATE = (
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
    '    {"action": "execute_runner|route_capability|takeover|report_blocker", "run_id": "", "priority": 1, "reason": ""}\n'
    "  ],\n"
    '  "blockers": [],\n'
    '  "risks": [],\n'
    '  "notes": []\n'
    "}\n"
    "[/PARENT_PLANNER_RESULT]\n"
)


# ---------------------------------------------------------------------------
# Planner state collection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlannerInputContext:
    tasks: list
    active_tasks: list
    due_report: Any
    action_plan: Any
    runner_candidates: list
    patch_run_ids: list
    open_requests: list
    open_gaps: list


@dataclass(frozen=True)
class PlannerStateParams:
    cfg: CapabilityConfig
    max_runners: int
    limit: int
    reviewer: str
    note: str


@dataclass(frozen=True)
class PlannerPromptParams:
    execution_plan: DispatchExecutionPlan
    runner_instruction: str


def build_parent_planner_state(
    agent: SimpleAgent,
    cfg: CapabilityConfig | None = None,
    *,
    params: PlannerStateParams | None = None,
    max_runners: int = 1,
    limit: int = 20,
    reviewer: str = "",
    note: str = "",
) -> dict[str, Any]:
    params = params or PlannerStateParams(
        cfg=cfg or CapabilityConfig(),
        max_runners=max_runners,
        limit=limit,
        reviewer=reviewer,
        note=note,
    )
    board, ctx = _collect_parent_planner_context(agent, params)
    gate_summary = _build_gate_summary(ctx)
    return _build_planner_state_dict(gate_summary, board, ctx, params.limit)


def _collect_parent_planner_context(agent: SimpleAgent, params: PlannerStateParams) -> tuple[Any, PlannerInputContext]:
    from .runner.dispatch import (
        _dispatch_patch_review_run_ids,
        _dispatch_runner_candidates,
        _limit_items,
    )

    tasks = agent.subagents.list_runs()
    board_limit = params.limit if params.limit > 0 else len(tasks)
    board = agent.subagents.board.build_board(options=SubAgentBoardOptions(recent_limit=board_limit))
    due_report = agent.subagents.board.due_check(params=SubAgentDueCheckOptions(config=params.cfg))
    action_plan = agent.subagents.board.plan_actions(params.cfg)
    runner_candidates = _dispatch_runner_candidates(tasks, params.max_runners)
    patch_run_ids = _limit_items(_dispatch_patch_review_run_ids(tasks), params.limit)
    active_tasks = _active_planner_tasks(tasks)
    open_requests, open_gaps = _collect_open_capability_items(tasks)
    return (
        board,
        PlannerInputContext(
            tasks=tasks,
            active_tasks=active_tasks,
            due_report=due_report,
            action_plan=action_plan,
            runner_candidates=runner_candidates,
            patch_run_ids=patch_run_ids,
            open_requests=open_requests,
            open_gaps=open_gaps,
        ),
    )


def _active_planner_tasks(tasks: list) -> list:
    terminal_statuses = {"DONE", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER"}
    return [task for task in tasks if task.status not in terminal_statuses]


def _collect_open_capability_items(tasks):
    open_requests = []
    open_gaps = []
    for task in tasks:
        open_requests.extend(_open_capability_request_items(task))
        open_gaps.extend(_open_capability_gap_items(task))
    return open_requests, open_gaps


def _open_capability_request_items(task) -> list[dict[str, object]]:
    return [
        item
        for request in task.capability_requests
        if (item := _open_capability_request_item(task, request)) is not None
    ]


def _open_capability_gap_items(task) -> list[dict[str, object]]:
    return [
        item
        for gap in task.capability_gaps
        if (item := _open_capability_gap_item(task, gap)) is not None
    ]


def _open_capability_request_item(task, request) -> dict[str, object] | None:
    if request.status != "OPEN":
        return None
    return {
        "run_id": task.id,
        "request_id": request.id,
        "needed_capability": request.needed_capability,
        "problem": request.problem,
        "expected_output": request.expected_output,
    }


def _open_capability_gap_item(task, gap) -> dict[str, object] | None:
    if gap.status != "OPEN":
        return None
    return {
        "run_id": task.id,
        "gap_id": gap.id,
        "needed_capability": gap.needed_capability,
        "problem": gap.problem,
    }


def _build_gate_summary(ctx: PlannerInputContext) -> dict[str, Any]:
    gate_summary = {
        "total_tasks": len(ctx.tasks),
        "active_tasks": len(ctx.active_tasks),
        "due_issues": ctx.due_report.summary.get("total", 0),
        "action_items": ctx.action_plan.summary.get("total", 0),
        "runner_candidates": len(ctx.runner_candidates),
        "patch_reviews": len(ctx.patch_run_ids),
        "open_capability_requests": len(ctx.open_requests),
        "open_capability_gaps": len(ctx.open_gaps),
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
                "open_capability_requests",
                "open_capability_gaps",
            )
        )
    )
    return gate_summary


def _build_planner_state_dict(gate_summary: dict[str, Any], board: Any, ctx: PlannerInputContext, limit: int) -> dict[str, Any]:
    from .runner.dispatch import _limit_items

    return {
        "gate": gate_summary,
        "board_summary": board.summary,
        "active_tasks": [_task_state_for_planner(task) for task in _limit_items(ctx.active_tasks, limit)],
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
            for issue in _limit_items(ctx.due_report.issues, limit)
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
            for item in _limit_items(ctx.action_plan.actions, limit)
        ],
        "runner_candidates": [_task_state_for_planner(task) for task in ctx.runner_candidates],
        "patch_review_run_ids": ctx.patch_run_ids,
        "open_capability_requests": _limit_items(ctx.open_requests, limit),
        "open_capability_gaps": _limit_items(ctx.open_gaps, limit),
    }


def task_state_for_planner(task) -> dict[str, Any]:
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


_task_state_for_planner = task_state_for_planner


def build_parent_planner_prompt(
    state: dict[str, Any],
    *,
    params: PlannerPromptParams | None = None,
    apply: bool = False,
    start_runners: bool = False,
    max_runners: int = 1,
    runner_instruction: str = "",
) -> str:
    import json

    params = params or PlannerPromptParams(
        DispatchExecutionPlan.from_parts(
            mutate_state=apply,
            start_runners=start_runners,
            max_runners=max_runners,
        ),
        runner_instruction,
    )
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    mode = "apply" if params.execution_plan.mutate_state else "dry-run"
    return _parent_planner_prompt(payload, mode, params)


def _parent_planner_prompt(payload: str, mode: str, params: PlannerPromptParams) -> str:
    return (
        "# Parent Planner Tick\n\n"
        "你是父代理 planner。这个 tick 来自定时 watch，不是浅层 heartbeat。\n"
        "请根据状态快照判断是否有待处理事项；如果有 active/pending/stalled/"
        "needs-intervention，应返回推进建议，而不是空心 HEARTBEAT_OK。\n\n"
        "你不能调用工具；State Snapshot 是本轮唯一事实来源。真正写回由调度器按审计流程执行。\n\n"
        "## Runtime\n\n"
        f"- mode: {mode}\n"
        f"- start_runners: {params.execution_plan.start_runners}\n"
        f"- cli_max_runners: {params.execution_plan.max_runners}\n"
        f"- existing_runner_instruction: {params.runner_instruction or 'none'}\n\n"
        "## State Snapshot\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n\n"
        "## Decision Rules\n\n"
        "- 如果 gate.needs_planner 为 0，可以返回 HEARTBEAT_OK。\n"
        "- 如果 gate.needs_planner 为 1，应给出 DISPATCH 或 BLOCKED_REPORT。\n"
        "- 你可以建议 runner_instruction，但不能提高 cli_max_runners，只能建议更小或相等的数量。\n"
        "- 你输出的 actions 只是建议；系统会再用规则调度器验证和执行。\n\n"
        f"{PARENT_PLANNER_RESULT_TEMPLATE}"
    )


def combine_runner_instruction(base: str, planner_instruction: str) -> str:
    base = base.strip()
    planner_instruction = planner_instruction.strip()
    if base and planner_instruction:
        return f"{base}\n\n父代理 planner 补充指令：{planner_instruction}"
    return base or planner_instruction
