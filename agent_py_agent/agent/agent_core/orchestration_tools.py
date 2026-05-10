# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""exposes model-callable orchestration tools backed by SimpleAgent subagent workflows.

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
from .hierarchy_tools import ScheduleChildSubagentsTool
from .orchestration_dispatch_payload import dispatch_record_payload
from .orchestration_dispatch_scope import (
    dispatch_apply_default,
    dispatch_auto_apply_acceptance_followup_default,
    dispatch_exclude_run_ids,
    dispatch_execute_acceptance_tests_default,
    dispatch_execute_runners_default,
    dispatch_finalize_acceptance,
    dispatch_max_runners_default,
    dispatch_parent_run_id,
    dispatch_workflow_mode,
)
from .orchestration_progress_payload import direct_children_progress_payload
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


# LLM: _tool_workflow_mode 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理工具工作流mode相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _tool_workflow_mode(explicit_mode: object, config_mode: object) -> str:
    if isinstance(explicit_mode, str):
        normalized = explicit_mode.strip().lower()
        if normalized in {"off", "plan", "auto"}:
            return normalized
        if normalized:
            return "off"
    if isinstance(config_mode, str):
        normalized = config_mode.strip().lower()
        if normalized == "auto":
            return "auto"
        if normalized == "manual":
            return "plan"
    return "off"


# LLM: _subagent_allowed_tools 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理子代理allowed工具相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _subagent_allowed_tools(params: dict[str, object]) -> list[str] | None:
    allowed_tools = _string_list(params.get("allowed_tools"))
    if allowed_tools:
        return allowed_tools
    if "tool_preset" not in params:
        return None
    preset = str(params.get("tool_preset") or "read_only").strip().lower()
    if preset == "coding":
        return list(CODING_SUBAGENT_TOOLS)
    if preset == "none":
        return []
    return list(READ_ONLY_SUBAGENT_TOOLS)


# LLM: _create_run_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建参数所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _create_run_params(
    agent,
    raw_params: dict[str, object],
    goal: str,
    allowed_tools: list[str] | None,
):
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


# LLM: _merged_extra_write_roots 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 更新mergedextrawriteroots对应的任务或运行状态，并保留既有字段语义；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _merged_extra_write_roots(params: dict[str, object], goal: str) -> list[str]:
    roots: list[str] = []
    for item in [*_string_list(params.get("extra_write_roots")), *_extract_write_dirs(goal)]:
        text = str(item or "").strip()
        if text and text not in roots:
            roots.append(text)
    return roots


# LLM: CreateSubagentsTool 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 提供create子代理工具模型工具入口，把结构化参数转为子代理操作；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class CreateSubagentsTool(BaseTool):

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_create_subagents_spec()

    # LLM: execute 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进execute的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
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
        target_error = external_write_target_error(
            self.agent,
            goal,
            allowed_tools or CODING_SUBAGENT_TOOLS,
        )
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

    # LLM: _requested_count 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 发送requested数量请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def _requested_count(self, params: dict[str, object]) -> int | ToolExecutionResult:
        count = _positive_int(params.get("count"), default=1)
        if count <= 0:
            return ToolExecutionResult("create_subagents", False, "count 必须大于 0。")
        if self.agent.config.max_subagents > 0:
            count = min(count, self.agent.config.max_subagents)
        return count

    # LLM: _create_tasks 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建tasks所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
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

    # LLM: _create_payload 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建载荷所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _create_payload(self, tasks, allowed_tools: list[str] | None) -> dict[str, object]:
        return {
            "created": len(tasks),
            "ids": [task.id for task in tasks],
            "allowed_tools": allowed_tools or "automatic",
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


# LLM: SubagentBoardTool 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 提供子代理看板工具模型工具入口，把结构化参数转为子代理操作；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class SubagentBoardTool(BaseTool):

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_subagent_board_spec()

    # LLM: execute 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进execute的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
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


# LLM: DispatchSubagentsTool 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 提供调度子代理工具模型工具入口，把结构化参数转为子代理操作；关键副作用: 方法可能触发运行循环、工具调用、调度记录和最终响应相关副作用，需保持公开契约稳定。
class DispatchSubagentsTool(BaseTool):

    # LLM: __init__ 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_dispatch_subagents_spec()

    # LLM: execute 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进execute的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        apply = dispatch_apply_default(self.agent, params)
        execute_runners = dispatch_execute_runners_default(self.agent, params, apply=apply)
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

    # LLM: _router 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理router相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
    def _router(self) -> tuple[CapabilityConfig, CapabilityRouter]:
        cfg = CapabilityConfig()
        tool_specs = [spec for spec in self.agent.tools.specs() if spec.category != "orchestration"]
        return cfg, CapabilityRouter(config=cfg, tool_specs=tool_specs)

    # LLM: _dispatch_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 推进参数的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
    def _dispatch_params(
        self,
        params: dict[str, object],
        apply: bool,
        execute_runners: bool,
        ) -> DispatchParams:
        execute_acceptance_tests = dispatch_execute_acceptance_tests_default(self.agent, params, apply=apply)
        return DispatchParams(
            apply=apply,
            execute_runners=execute_runners,
            planner=_bool_param(params.get("planner"), default=False),
            workflow_mode=dispatch_workflow_mode(self.agent, params, _tool_workflow_mode),
            max_runners=dispatch_max_runners_default(self.agent, params),
            limit=_non_negative_int(params.get("limit"), default=20),
            reviewer=str(params.get("reviewer") or "chat-tool").strip(),
            note=str(params.get("note") or "triggered by dispatch_subagents tool").strip(),
            runner_instruction=str(params.get("runner_instruction") or params.get("instruction") or "").strip(),
            max_cards=_non_negative_int(params.get("max_cards"), default=0),
            probe=not _bool_param(params.get("no_probe"), default=False),
            take_over_by=str(params.get("take_over_by") or "").strip(),
            locked_files=_string_list(params.get("locked_files")),
            execute_acceptance_tests=execute_acceptance_tests,
            auto_apply_acceptance_followup=dispatch_auto_apply_acceptance_followup_default(
                self.agent,
                params,
                apply=apply,
                execute_acceptance_tests=execute_acceptance_tests,
            ),
            parent_run_id=dispatch_parent_run_id(self.agent, params),
            root_id=str(params.get("root_id") or "").strip(),
            exclude_run_ids=dispatch_exclude_run_ids(self.agent, params),
            finalize_acceptance=dispatch_finalize_acceptance(self.agent, params),
        )

    # LLM: _report_payload 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 处理报告载荷相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _report_payload(self, report) -> dict[str, object]:
        payload = {
            "dry_run": report.dry_run,
            "summary": report.summary,
            "records": [dispatch_record_payload(item) for item in report.records],
            "dispatch_json": str(self.agent.subagents.workspace / "subagent_dispatch_report.json"),
            "dispatch_md": str(self.agent.subagents.workspace / "SUBAGENT_DISPATCH.md"),
        }
        payload.update(direct_children_progress_payload(self.agent))
        return payload
