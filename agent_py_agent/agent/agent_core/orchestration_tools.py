# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""exposes model-callable orchestration tools backed by SimpleAgent subagent workflows.

这些不是普通文件工具，而是'主代理让模型触发子代理流程'的工具。
创建子代理、查看看板、执行 dispatch 都在这里，真实业务再转给 SimpleAgent 和 SubAgentManager。
"""

import json
from typing import TYPE_CHECKING

from ..action_protocol import subagent_schedule_envelope_from_payload
from ..subagents.models import SubAgentBoardOptions
from ..subagents.services.base import CreateRunParams
from ..tools import BaseTool, ToolExecutionResult
from .hierarchy_tools import ScheduleChildSubagentsTool
from .orchestration_board_payload import (
    board_actionable_run_ids,
    board_completion_status,
    board_kernel_snapshot_payload,
    board_status_filter,
    clip_board_text,
    scoped_board_items,
)
from .orchestration_create_constraints import (
    ambiguous_repeated_product_goal_error,
    delegation_constraint_conflict_error,
    explicit_root_missing_write_root_error,
)
from .orchestration_create_policy import (
    create_run_params,
)
from .orchestration_dispatch_tool import DispatchSubagentsTool
from .orchestration_run_scope import remember_orchestration_run_ids
from .orchestration_tool_grants import (
    CODING_SUBAGENT_TOOLS,
    READ_ONLY_SUBAGENT_TOOLS,
    subagent_allowed_tools,
)
from .orchestration_tool_specs import (
    build_create_subagents_spec,
    build_subagent_board_spec,
)
from .orchestration_workflow_mode import tool_workflow_mode as _tool_workflow_mode
from .orchestration_write_guard import external_write_target_error
from .parameters import _positive_int

if TYPE_CHECKING:
    from ..core import SimpleAgent


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

        allowed_tools = subagent_allowed_tools(params)
        missing_write_root = explicit_root_missing_write_root_error(params, goal)
        if missing_write_root:
            return ToolExecutionResult("create_subagents", False, missing_write_root)
        target_error = external_write_target_error(
            self.agent,
            goal,
            allowed_tools or CODING_SUBAGENT_TOOLS,
        )
        if target_error:
            return ToolExecutionResult("create_subagents", False, target_error)
        constraint_conflict = delegation_constraint_conflict_error(self.agent, goal)
        if constraint_conflict:
            return ToolExecutionResult("create_subagents", False, constraint_conflict)

        run_params = create_run_params(self.agent, params, goal, allowed_tools)
        ambiguous_product_count = ambiguous_repeated_product_goal_error(goal, count, run_params.role)
        if ambiguous_product_count:
            return ToolExecutionResult("create_subagents", False, ambiguous_product_count)
        tasks = self._create_tasks(goal, count, run_params)
        remember_orchestration_run_ids(self.agent, [task.id for task in tasks])
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
            task_goal = run_params.goal if count == 1 else f"{run_params.goal} / 子任务{index}"
            task_params = CreateRunParams(**{**run_params.__dict__, "goal": task_goal})
            task = self.agent.subagents.create_run(
                params=task_params,
            )
            tasks.append(task)
        return tasks

    # LLM: _create_payload 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
    # 函数用途: 构建载荷所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _create_payload(self, tasks, allowed_tools: list[str] | None) -> dict[str, object]:
        payload: dict[str, object] = {
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
        payload["typed_envelope"] = subagent_schedule_envelope_from_payload(
            payload,
            tool="create_subagents",
        ).to_dict()
        return payload


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
        status_filter = board_status_filter(params.get("status"))
        board = self.agent.subagents.write_board(
            options=SubAgentBoardOptions(recent_limit=max(1, limit)),
        )
        items = scoped_board_items(self.agent, board.items)
        if status_filter:
            items = [item for item in items if item.status.upper() == status_filter]
        items = items[:limit]
        payload = {
            "summary": board.summary,
            "completion_status": board_completion_status(items),
            "kernel_snapshot": board_kernel_snapshot_payload(self.agent, items),
            "returned": len(items),
            "actionable_run_ids": board_actionable_run_ids(items),
            "subagent_workspace": str(self.agent.subagents.workspace),
            "items": [
                {
                    "id": item.id,
                    "root_id": str(getattr(item, "root_id", "") or ""),
                    "parent_id": str(getattr(item, "parent_id", "") or ""),
                    "depth": int(getattr(item, "depth", 0) or 0),
                    "agent_name": str(getattr(item, "agent_name", "") or ""),
                    "role": str(getattr(item, "role", "") or ""),
                    "goal": clip_board_text(item.goal),
                    "status": item.status,
                    "verification_status": item.verification_status,
                    "channel_status": item.channel_status,
                    "risk_flags": item.risk_flags,
                    "evidence_count": item.evidence_count,
                    "child_count": int(getattr(item, "child_count", 0) or 0),
                    "child_status_counts": dict(getattr(item, "child_status_counts", {}) or {}),
                    "open_request_count": item.open_request_count,
                    "open_gap_count": item.open_gap_count,
                    "latest_summary": clip_board_text(str(getattr(item, "latest_summary", "") or ""), limit=180),
                    "blocker_count": int(getattr(item, "blocker_count", 0) or 0),
                    "target_tokens": list(getattr(item, "target_tokens", []) or []),
                    "task_dir": item.task_dir,
                    "output_json": str(getattr(item, "output_json", "") or ""),
                }
                for item in items
            ],
            "board_json": str(self.agent.subagents.workspace / "subagent_board.json"),
            "board_md": str(self.agent.subagents.workspace / "SUBAGENT_BOARD.md"),
        }
        return ToolExecutionResult("subagent_board", True, json.dumps(payload, ensure_ascii=False, indent=2))
