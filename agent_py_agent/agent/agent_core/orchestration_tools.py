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
from ..subagents.services.base import CreateRunParams, _extract_write_dirs
from ..tools import BaseTool, ToolExecutionResult, ToolSpec
from .dispatch_params import DispatchParams
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


def _create_run_params(agent, params: dict[str, object], goal: str, allowed_tools: list[str]):
    workflow_mode = _tool_workflow_mode(params.get("workflow_mode"), agent.config.subagent_workflow_mode)
    extra_write_roots = _merged_extra_write_roots(params, goal)
    return CreateRunParams(
        goal=goal,
        thought=str(params.get("thought") or "根据父代理派工执行，并保留可验收证据。").strip(),
        plan=_string_list(params.get("plan")) or ["理解目标", "执行任务", "产出证据", "等待父代理验收"],
        agent_name=str(params.get("agent_name") or "general").strip(),
        role=str(params.get("role") or "worker").strip(),
        allowed_tools=allowed_tools,
        owner=str(params.get("owner") or "").strip(),
        supervisor=str(params.get("supervisor") or "parent").strip(),
        final_owner=str(params.get("final_owner") or "").strip(),
        acceptance_checks=_string_list(params.get("acceptance_checks")),
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
        self.spec = ToolSpec(
            name="create_subagents",
            category="orchestration",
            description="创建一个或多个子代理任务记录，适合把复杂任务正式拆给子代理。",
            use_cases=[
                "用户要求拆分任务、派多个子代理、开工单或让子代理分别处理事项",
                "需要把聊天里的计划落盘，后续由 dispatch_subagents 推进和验收",
            ],
            avoid_when=[
                "只是解释思路、不需要真正创建任务时，不要调用；先直接回答即可",
            ],
            keywords=[
                "子代理",
                "派工",
                "拆分",
                "工单",
                "任务",
                "subagent",
                "delegate",
                "spawn",
                "assign",
            ],
            parameters={
                "goal": "总目标或任务描述，必填",
                "count": "创建多少个子代理，默认 1，受 max_subagents 限制",
                "tool_preset": "默认 read_only；coding 会授予文件读写工具；none 不授予工具",
                "allowed_tools": "显式工具列表；传了它就覆盖 tool_preset",
                "acceptance_checks": "验收标准列表",
                "plan": "每个子代理的初始步骤列表",
                "workflow_mode": "off/plan/auto；决定是否在建工单时挂 workflow 计划",
                "extra_write_roots": "额外写入目录列表；目录必须位于 workspace_root 列表允许范围内",
            },
            parameter_details={
                "goal": "写清楚子代理要交付什么，不要只写一个空泛标题。",
                "count": "例如 3 表示创建 3 个并列子任务；如果任务需要人工精细拆分，可以多次调用本工具。",
                "tool_preset": "`read_only` 只允许 list/read/search；`coding` 允许读写和替换文件；`none` 不授予工具。",
                "allowed_tools": "JSON 数组，例如 [\"read_file\", \"write_file\"]。如果需要写代码，通常至少给 read_file/search_text/write_file/replace_in_file。",
                "acceptance_checks": "JSON 数组或多行文本，说明父代理后续怎样判断任务完成。",
                "plan": "JSON 数组或多行文本，给子代理的初始执行步骤。",
                "workflow_mode": "默认跟随配置：auto->auto，manual->plan，off->off。显式传值会覆盖配置。",
                "extra_write_roots": "JSON 数组，例如 [\"C:/Users/you/Desktop/work\"]；只给本次子代理任务增加写入边界。",
            },
            examples=[
                '{"tool":"create_subagents","goal":"在隔离 fixture 项目里实现三个小功能并写报告","count":3,"tool_preset":"coding","workflow_mode":"auto","acceptance_checks":["必须有文件证据","必须说明测试结果"]}',
                '{"tool":"create_subagents","goal":"调研 gateway 失败场景","count":2,"tool_preset":"read_only"}',
            ],
        )

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
        self.spec = ToolSpec(
            name="subagent_board",
            category="orchestration",
            description="查看当前子代理看板和状态摘要，用来判断任务是否待执行、待验收或卡住。",
            use_cases=[
                "用户问当前任务进度、有哪些子代理、哪些任务卡住或完成",
                "调度前先查看任务树状态，避免重复派工",
            ],
            avoid_when=[
                "已经知道具体 run_id 且只需要执行 dispatch 时，可以直接调用 dispatch_subagents",
            ],
            keywords=["任务状态", "看板", "进度", "子代理", "board", "status", "subagent"],
            parameters={
                "limit": "最多返回多少条明细，默认 10",
                "status": "按状态过滤，可选，如 PLANNING/DONE/BLOCKED",
            },
            examples=[
                '{"tool":"subagent_board","limit":10}',
                '{"tool":"subagent_board","status":"BLOCKED","limit":20}',
            ],
        )

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        limit = _positive_int(params.get("limit"), default=10)
        status_filter = str(params.get("status") or "").strip().upper()
        board = self.agent.subagents.write_board(recent_limit=max(1, limit))
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
        self.spec = ToolSpec(
            name="dispatch_subagents",
            category="orchestration",
            description="执行一轮子代理调度，可 dry-run，也可 apply 并调用真实 runner。",
            use_cases=[
                "已经创建子代理后，用户要求推进、开跑、验收、处理卡住项",
                "需要让父代理检查 due-check、路由能力、执行 runner、审核 patch 或验收结果",
            ],
            avoid_when=[
                "只是创建任务时先用 create_subagents；没有明确推进意图时默认 dry-run 更稳",
            ],
            keywords=["调度", "推进", "运行", "验收", "派工", "dispatch", "runner", "acceptance"],
            parameters={
                "apply": "是否写回低风险动作，默认 false",
                "execute_runners": "是否真实调用模型执行 runner，必须配合 apply=true",
                "planner": "是否启用父代理 planner，默认 false",
                "workflow_mode": "off/plan/auto；是否在 dispatch 前补做 workflow 规划或自动派工",
                "max_runners": "本轮最多推进多少个 runner，默认 1；0 表示不执行 runner",
                "limit": "每阶段最多处理多少条记录，默认 20；0 表示不限制",
                "runner_instruction": "给 runner 的额外指令",
            },
            parameter_details={
                "apply": "false 只生成计划和报告；true 会写审计日志并可能改变任务状态。",
                "execute_runners": "true 会消耗真实 API；只有用户明确要求开跑/真实执行/完整测试时才打开。",
                "planner": "true 会额外调用父代理 LLM planner；适合长任务统筹，但会多消耗一次模型调用。",
                "workflow_mode": "plan 只把 workflow 计划写回父任务；auto 会在计划 OK 时落成 worker 子工单。",
                "max_runners": "用来限制本轮推进数量，避免一次把太多子代理同时跑起来。",
            },
            examples=[
                '{"tool":"dispatch_subagents","apply":false,"workflow_mode":"plan","max_runners":1}',
                '{"tool":"dispatch_subagents","apply":true,"execute_runners":true,"workflow_mode":"auto","max_runners":2,"runner_instruction":"只在隔离 fixture 目录内写文件，并输出可验收证据"}',
            ],
        )

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
