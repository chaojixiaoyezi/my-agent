# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""exposes model-callable orchestration tools backed by SimpleAgent subagent workflows.

这些不是普通文件工具，而是'主代理让模型触发子代理流程'的工具。
创建子代理、查看看板、执行 dispatch 都在这里，真实业务再转给 SimpleAgent 和 SubAgentManager。
"""

import json
from typing import TYPE_CHECKING

from ..subagents.models import SubAgentBoardOptions
from ..subagents.services.base import CreateRunParams
from ..tools import BaseTool, ToolExecutionResult
from .hierarchy_tools import ScheduleChildSubagentsTool
from .orchestration_board_payload import (
    board_actionable_run_ids,
    board_completion_status,
    board_kernel_snapshot_payload,
)
from .orchestration_board_tool_payload import (
    board_items_for_payload,
    board_payload_item,
    board_ref_preview,
)
from .orchestration_create_constraints import (
    ambiguous_repeated_product_goal_error,
    delegation_constraint_conflict_error,
    explicit_root_missing_write_root_error,
)
from .orchestration_create_idempotency import (
    CreateTaskResolution,
    resolve_create_run,
)
from .orchestration_create_items import CreateSubagentItem, create_items_from_params
from .orchestration_create_payload import create_subagents_payload
from .orchestration_create_policy import (
    create_run_params,
)
from .orchestration_dispatch_tool import DispatchSubagentsTool
from .orchestration_item_dependencies import enrich_item_dependencies, item_dependency_edges
from .orchestration_lineage_names import indexed_count_params, indexed_item_params
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
from .orchestration_write_guard import ExternalWriteTargetRequest, external_write_target_error
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

        items_result = self._items_result(params)
        if items_result is not None:
            return items_result

        prepared = self._prepare_count_mode(params)
        if isinstance(prepared, ToolExecutionResult):
            return prepared
        goal, count, allowed_tools, run_params = prepared
        resolutions = self._create_tasks(goal, count, run_params)
        tasks = [item.task for item in resolutions]
        remember_orchestration_run_ids(self.agent, [task.id for task in tasks])
        payload = create_subagents_payload(self.agent, resolutions, allowed_tools, params)
        return ToolExecutionResult(
            "create_subagents",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    # LLM: _items_result routes structured batch mode before count-mode validation.
    # 函数用途: 解析 items/tasks 入口；返回 None 表示继续单 goal/count 模式。
    def _items_result(self, params: dict[str, object]) -> ToolExecutionResult | None:
        items = create_items_from_params(params)
        if isinstance(items, str):
            return ToolExecutionResult("create_subagents", False, items)
        if items:
            return self._execute_items(items)
        return None

    # LLM: _prepare_count_mode validates single-goal delegation and builds run params.
    # 函数用途: 聚合 count 模式的 goal、count、工具和 run 参数，保持 execute 入口薄。
    def _prepare_count_mode(
        self, params: dict[str, object]
    ) -> tuple[str, int, list[str] | None, CreateRunParams] | ToolExecutionResult:
        goal = str(params.get("goal") or "").strip()
        if not goal:
            return ToolExecutionResult("create_subagents", False, "缺少必填参数 goal。")
        count = self._requested_count(params)
        if isinstance(count, ToolExecutionResult):
            return count
        allowed_tools = subagent_allowed_tools(params)
        validation = self._validate_single_goal(params, goal, allowed_tools)
        if validation:
            return ToolExecutionResult("create_subagents", False, validation)
        run_params = create_run_params(self.agent, params, goal, allowed_tools)
        ambiguous = ambiguous_repeated_product_goal_error(params, count, run_params.role)
        if ambiguous:
            return ToolExecutionResult("create_subagents", False, ambiguous)
        return goal, count, allowed_tools, run_params

    # LLM: _execute_items is the structured batch path, equivalent to Hermes delegate_task tasks[].
    # 函数用途: 按 items[] 中每个独立 goal 创建子代理，避免 count 复制同一个任务目标。
    def _execute_items(self, items: list[CreateSubagentItem]) -> ToolExecutionResult:
        capped_raw = self._cap_items(items)
        dependency_edges = item_dependency_edges(capped_raw)
        capped = enrich_item_dependencies(capped_raw)
        allowed_tool_values = [subagent_allowed_tools(item.params) for item in capped]
        for item, allowed_tools in zip(capped, allowed_tool_values, strict=True):
            validation = self._validate_single_goal(item.params, item.goal, allowed_tools)
            if validation:
                return ToolExecutionResult("create_subagents", False, validation)
        resolutions: list[CreateTaskResolution] = []
        for index, item in enumerate(capped, start=1):
            run_params = create_run_params(
                self.agent,
                item.params,
                item.goal,
                subagent_allowed_tools(item.params),
            )
            resolution = resolve_create_run(
                self.agent.subagents,
                indexed_item_params(run_params, index=index, total=len(capped)),
            )
            resolutions.append(resolution)
        tasks = [item.task for item in resolutions]
        _apply_item_dependency_edges(self.agent.subagents, tasks, dependency_edges)
        remember_orchestration_run_ids(self.agent, [task.id for task in tasks])
        payload = create_subagents_payload(
            self.agent,
            resolutions,
            _payload_allowed_tools(allowed_tool_values),
            {"items": [item.params for item in capped]},
        )
        payload["batch_mode"] = "items"
        return ToolExecutionResult(
            "create_subagents",
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    # LLM: _cap_items applies the same user-configured fan-out ceiling as count mode.
    # 函数用途: 避免 items[] 绕过 max_subagents；配置为 0 或更小时表示不限制。
    def _cap_items(self, items: list[CreateSubagentItem]) -> list[CreateSubagentItem]:
        max_subagents = int(getattr(self.agent.config, "max_subagents", 0) or 0)
        if max_subagents > 0:
            return items[:max_subagents]
        return items

    # LLM: _validate_single_goal centralizes per-child create checks for count and items modes.
    # 函数用途: 对单个子代理目标做写入边界和约束检查，返回空字符串表示可创建。
    def _validate_single_goal(
        self,
        params: dict[str, object],
        goal: str,
        allowed_tools: list[str] | None,
    ) -> str:
        missing_write_root = explicit_root_missing_write_root_error(self.agent, params, goal)
        if missing_write_root:
            return missing_write_root
        target_error = external_write_target_error(
            ExternalWriteTargetRequest(
                agent=self.agent,
                allowed_tools=allowed_tools or CODING_SUBAGENT_TOOLS,
                params=params,
            )
        )
        if target_error:
            return target_error
        return delegation_constraint_conflict_error(params)

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
    def _create_tasks(self, goal: str, count: int, run_params: CreateRunParams) -> list[CreateTaskResolution]:
        resolutions: list[CreateTaskResolution] = []
        for index in range(1, count + 1):
            task_params = indexed_count_params(run_params, index=index, count=count)
            resolutions.append(resolve_create_run(self.agent.subagents, task_params))
        return resolutions

# LLM: _apply_item_dependency_edges persists batch sibling ordering as workflow-style phase refs.
# 函数用途: items[] 下游自然引用上游代理时，写入 run 级依赖，dispatch 显式 run_ids 也不能抢跑。
def _apply_item_dependency_edges(manager, tasks: list, dependency_edges: list[list[int]]) -> None:
    if not tasks:
        return
    batch_id = f"items:{tasks[0].id}"
    for task, deps in zip(tasks, dependency_edges, strict=False):
        task.workflow_parent_run_id = batch_id
        task.workflow_phase_id = task.id
        task.workflow_depends_on = [tasks[index].id for index in deps if 0 <= index < len(tasks)]
        manager.save(task)


# LLM: _payload_allowed_tools summarizes items-mode tool policy without hiding per-task params.
# 函数用途: 多个 item 工具策略一致时保留原输出；不一致时告诉模型每个任务独立决定。
def _payload_allowed_tools(values: list[list[str] | None]) -> list[str] | str | None:
    if not values:
        return None
    first = values[0]
    if all(value == first for value in values):
        return first
    return "per_item"


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
        board = self.agent.subagents.write_board(
            options=SubAgentBoardOptions(recent_limit=max(1, limit)),
        )
        items = board_items_for_payload(self.agent, board.items, params, limit)
        payload = {
            "summary": board.summary,
            "completion_status": board_completion_status(items),
            "kernel_snapshot": board_kernel_snapshot_payload(self.agent, items),
            "returned": len(items),
            "actionable_run_ids": board_actionable_run_ids(items),
            "deliverable_artifact_refs": board_ref_preview(items, "artifact_refs"),
            "deliverable_evidence_refs": board_ref_preview(items, "evidence_refs"),
            "subagent_workspace": str(self.agent.subagents.workspace),
            "items": [board_payload_item(item) for item in items],
            "board_json": str(self.agent.subagents.workspace / "subagent_board.json"),
            "board_md": str(self.agent.subagents.workspace / "SUBAGENT_BOARD.md"),
        }
        return ToolExecutionResult("subagent_board", True, json.dumps(payload, ensure_ascii=False, indent=2))
