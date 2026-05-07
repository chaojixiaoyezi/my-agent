from __future__ import annotations

"""LLM: exposes model-callable orchestration tools backed by SimpleAgent subagent workflows.

给人看的解释：
这些不是普通文件工具，而是'主代理让模型触发子代理流程'的工具。
创建子代理、查看看板、执行 dispatch 都在这里，真实业务再转给 SimpleAgent 和 SubAgentManager。
"""

import json
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagents.models import SubAgentBoardOptions
from ..subagents.services.base import CreateRunParams, _extract_write_dirs
from ..tools import BaseTool, ToolExecutionResult
from .dispatch_params import DispatchParams
from .orchestration_tool_specs import (
    build_create_subagents_spec,
    build_dispatch_subagents_spec,
    build_subagent_board_spec,
)
from .orchestration_write_guard import external_write_target_error
from .parameters import _bool_param, _non_negative_int, _positive_int, _string_list

if TYPE_CHECKING:
    from ..core import SimpleAgent


READ_ONLY_SUBAGENT_TOOLS = ["list_files", "read_file", "search_text"]
CODING_SUBAGENT_TOOLS = [
    "list_files",
    "read_file",
    "search_text",
    "write_file",
    "append_file",
    "replace_in_file",
]


def _tool_workflow_mode(explicit_mode: object, config_mode: object) -> str:
    if isinstance(explicit_mode, str):
        normalized = explicit_mode.strip().lower()
        if normalized in {"off", "plan", "auto"}:
            return normalized
    if isinstance(config_mode, str):
        normalized = config_mode.strip().lower()
        if normalized == "auto":
            return "auto"
        if normalized == "manual":
            return "plan"
    return "off"


def _subagent_allowed_tools(params: dict[str, object]) -> list[str]:
    allowed_tools = _string_list(params.get("allowed_tools"))
    if allowed_tools:
        return allowed_tools
    preset = str(params.get("tool_preset") or "read_only").strip().lower()
    if preset == "coding":
        return list(CODING_SUBAGENT_TOOLS)
    if preset == "none":
        return []
    return list(READ_ONLY_SUBAGENT_TOOLS)


def _create_run_params(agent, raw_params: dict[str, object], goal: str, allowed_tools: list[str]):
    workflow_mode = _tool_workflow_mode(raw_params.get("workflow_mode"), agent.config.subagent_workflow_mode)
    extra_write_roots = _merged_extra_write_roots(raw_params, goal)
    return CreateRunParams(
        goal=goal,
        thought=str(raw_params.get("thought") or "根据父代理派工执行，并保留可验收证据。").strip(),
        plan=_string_list(raw_params.get("plan")) or ["理解目标", "执行任务", "产出证据", "等待父代理验收"],
        agent_name=str(raw_params.get("agent_name") or "general").strip(),
        role=str(raw_params.get("role") or "worker").strip(),
        allowed_tools=allowed_tools,
        owner=str(raw_params.get("owner") or "").strip(),
        supervisor=str(raw_params.get("supervisor") or "parent").strip(),
        final_owner=str(raw_params.get("final_owner") or "").strip(),
        acceptance_checks=_string_list(raw_params.get("acceptance_checks")),
        extra_write_roots=extra_write_roots,
        workflow_mode=workflow_mode,
    )


def _merged_extra_write_roots(params: dict[str, object], goal: str) -> list[str]:
    roots: list[str] = []
    for item in [*_string_list(params.get("extra_write_roots")), *_extract_write_dirs(goal)]:
        text = str(item or "").strip()
        if text and text not in roots:
            roots.append(text)
    return roots


class CreateSubagentsTool(BaseTool):

    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_create_subagents_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        if not self.agent.config.enable_subagents:
            return ToolExecutionResult("create_subagents", False, "配置已禁用 subagent。")

        goal = str(params.get("goal") or "").strip()
        if not goal:
            return ToolExecutionResult("create_subagents", False, "缺少必填参数 goal。")

        count = self._requested_count(params)
        if isinstance(count, ToolExecutionResult):
            return count

        allowed_tools = _subagent_allowed_tools(params)
        target_error = external_write_target_error(self.agent, goal, allowed_tools)
        if target_error:
            return ToolExecutionResult("create_subagents", False, target_error)

        run_params = _create_run_params(self.agent, params, goal, allowed_tools)
        tasks = self._create_tasks(goal, count, run_params)
        payload = self._create_payload(tasks, allowed_tools)
        return ToolExecutionResult(
            "create_subagents",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def _requested_count(self, params: dict[str, object]) -> int | ToolExecutionResult:
        count = _positive_int(params.get("count"), default=1)
        if count <= 0:
            return ToolExecutionResult("create_subagents", False, "count 必须大于 0。")
        if self.agent.config.max_subagents > 0:
            count = min(count, self.agent.config.max_subagents)
        return count

    def _create_tasks(self, goal: str, count: int, run_params: CreateRunParams):
        tasks = []
        for index in range(1, count + 1):
            task_goal = goal if count == 1 else f"{goal} / 子任务{index}"
            task_params = CreateRunParams(**{**run_params.__dict__, "goal": task_goal})
            task = self.agent.subagents.create_run(
                params=task_params,
            )
            tasks.append(task)
        return tasks

    def _create_payload(self, tasks, allowed_tools: list[str]) -> dict[str, object]:
        return {
            "created": len(tasks),
            "ids": [task.id for task in tasks],
            "allowed_tools": allowed_tools,
            "subagent_workspace": str(self.agent.subagents.workspace),
            "tasks": [
                {
                    "id": task.id,
                    "goal": task.goal,
                    "status": task.status,
                    "verification_status": task.verification_status,
                    "task_dir": task.task_dir,
                }
                for task in tasks
            ],
        }


class SubagentBoardTool(BaseTool):

    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_subagent_board_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        limit = _positive_int(params.get("limit"), default=10)
        status_filter = str(params.get("status") or "").strip().upper()
        board = self.agent.subagents.write_board(
            options=SubAgentBoardOptions(recent_limit=max(1, limit)),
        )
        items = board.items
        if status_filter:
            items = [item for item in items if item.status.upper() == status_filter]
        items = items[:limit]
        payload = {
            "summary": board.summary,
            "returned": len(items),
            "subagent_workspace": str(self.agent.subagents.workspace),
            "items": [
                {
                    "id": item.id,
                    "goal": item.goal,
                    "status": item.status,
                    "verification_status": item.verification_status,
                    "channel_status": item.channel_status,
                    "risk_flags": item.risk_flags,
                    "evidence_count": item.evidence_count,
                    "open_request_count": item.open_request_count,
                    "open_gap_count": item.open_gap_count,
                    "task_dir": item.task_dir,
                }
                for item in items
            ],
            "board_json": str(self.agent.subagents.workspace / "subagent_board.json"),
            "board_md": str(self.agent.subagents.workspace / "SUBAGENT_BOARD.md"),
        }
        return ToolExecutionResult("subagent_board", True, json.dumps(payload, ensure_ascii=False, indent=2))


class DispatchSubagentsTool(BaseTool):

    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_dispatch_subagents_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        apply = _bool_param(params.get("apply"), default=False)
        execute_runners = _bool_param(params.get("execute_runners"), default=False)
        if execute_runners and not apply:
            return ToolExecutionResult(
                "dispatch_subagents",
                False,
                "execute_runners=true 必须配合 apply=true，避免误触发真实 API runner。",
            )

        cfg, router = self._router()
        report = self.agent.dispatch_subagents(
            router,
            cfg,
            params=self._dispatch_params(params, apply, execute_runners),
        )
        payload = self._report_payload(report)
        return ToolExecutionResult("dispatch_subagents", True, json.dumps(payload, ensure_ascii=False, indent=2))

    def _router(self) -> tuple[CapabilityConfig, CapabilityRouter]:
        cfg = CapabilityConfig()
        tool_specs = [spec for spec in self.agent.tools.specs() if spec.category != "orchestration"]
        return cfg, CapabilityRouter(config=cfg, tool_specs=tool_specs)

    def _dispatch_params(
        self,
        params: dict[str, object],
        apply: bool,
        execute_runners: bool,
    ) -> DispatchParams:
        return DispatchParams(
            apply=apply,
            execute_runners=execute_runners,
            planner=_bool_param(params.get("planner"), default=False),
            workflow_mode=_tool_workflow_mode(params.get("workflow_mode"), self.agent.config.subagent_workflow_mode),
            max_runners=_non_negative_int(params.get("max_runners"), default=1),
            limit=_non_negative_int(params.get("limit"), default=20),
            reviewer=str(params.get("reviewer") or "chat-tool").strip(),
            note=str(params.get("note") or "triggered by dispatch_subagents tool").strip(),
            runner_instruction=str(params.get("runner_instruction") or params.get("instruction") or "").strip(),
            max_cards=_non_negative_int(params.get("max_cards"), default=0),
            probe=not _bool_param(params.get("no_probe"), default=False),
            take_over_by=str(params.get("take_over_by") or "").strip(),
            locked_files=_string_list(params.get("locked_files")),
        )

    def _report_payload(self, report) -> dict[str, object]:
        return {
            "dry_run": report.dry_run,
            "summary": report.summary,
            "records": [
                {
                    "step": item.step,
                    "action": item.action,
                    "run_id": item.run_id,
                    "ok": item.ok,
                    "dry_run": item.dry_run,
                    "applied": item.applied,
                    "message": item.message,
                    "before_status": item.before_status,
                    "after_status": item.after_status,
                }
                for item in report.records
            ],
            "dispatch_json": str(self.agent.subagents.workspace / "subagent_dispatch_report.json"),
            "dispatch_md": str(self.agent.subagents.workspace / "SUBAGENT_DISPATCH.md"),
        }
