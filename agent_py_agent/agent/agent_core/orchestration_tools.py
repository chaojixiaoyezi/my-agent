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
from ..subagents.services.base import CreateRunParams, _extract_write_dirs
from ..tools import BaseTool, ToolExecutionResult
from .coordinator_seed_tools import explicit_root_allowed_tools
from .hierarchy_tools import ScheduleChildSubagentsTool
from .orchestration_board_payload import (
    board_actionable_run_ids,
    board_status_filter,
    clip_board_text,
)
from .orchestration_dispatch_tool import DispatchSubagentsTool
from .orchestration_root_contract import explicit_root_goal_with_user_contract
from .orchestration_tool_specs import (
    build_create_subagents_spec,
    build_subagent_board_spec,
)
from .orchestration_workflow_mode import tool_workflow_mode as _tool_workflow_mode
from .orchestration_write_guard import external_write_target_error
from .parameters import _positive_int, _string_list
from .spawn_role_seed import is_explicit_root_role

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
    if preset == "read_only":
        return list(READ_ONLY_SUBAGENT_TOOLS)
    if preset == "none":
        return []
    return None


# LLM: _create_run_params 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建参数所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _create_run_params(
    agent,
    raw_params: dict[str, object],
    goal: str,
    allowed_tools: list[str] | None,
):
    workflow_mode = _tool_workflow_mode(raw_params.get("workflow_mode"), agent.config.subagent_workflow_mode)
    role = str(raw_params.get("role") or "worker").strip()
    is_explicit_root = is_explicit_root_role(role)
    if is_explicit_root:
        workflow_mode = "off"
        allowed_tools = explicit_root_allowed_tools(allowed_tools)
        goal = explicit_root_goal_with_user_contract(agent, goal)
    extra_write_roots = _merged_extra_write_roots(raw_params, goal)
    return CreateRunParams(
        goal=goal,
        thought=str(raw_params.get("thought") or "根据父代理派工执行，并保留可验收证据。").strip(),
        plan=_string_list(raw_params.get("plan")) or ["理解目标", "执行任务", "产出证据", "等待父代理验收"],
        agent_name=_root_agent_name(raw_params, role),
        role=role,
        allowed_tools=allowed_tools,
        owner=str(raw_params.get("owner") or "").strip(),
        supervisor=str(raw_params.get("supervisor") or "parent").strip(),
        final_owner=str(raw_params.get("final_owner") or "").strip(),
        acceptance_checks=_string_list(raw_params.get("acceptance_checks")),
        extra_write_roots=extra_write_roots,
        workflow_mode=workflow_mode,
    )


# LLM: _root_agent_name gives top-level spawned agents the same lineage naming contract as descendants.
# 函数用途: create_subagents 未传 agent_name 时，用“小傻妞-role”兜底，避免真实测试落成 general。
def _root_agent_name(raw_params: dict[str, object], role: str) -> str:
    explicit = str(raw_params.get("agent_name") or "").strip().strip("-")
    if explicit:
        return explicit
    suffix = str(role or "worker").strip().replace("_", "-").strip("-") or "worker"
    if suffix in {"general", "child"}:
        suffix = "worker"
    return f"小傻妞-{suffix}"


# LLM: _merged_extra_write_roots 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 更新mergedextrawriteroots对应的任务或运行状态，并保留既有字段语义；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _merged_extra_write_roots(params: dict[str, object], goal: str) -> list[str]:
    roots: list[str] = []
    for item in [*_string_list(params.get("extra_write_roots")), *_extract_write_dirs(goal)]:
        text = str(item or "").strip()
        if text and text not in roots:
            roots.append(text)
    return roots


# LLM: explicit_root_missing_write_root_error prevents product paths from drifting into agent workspaces.
# 函数用途: 显式 root/coordinator 要交付文件但没带产物写入根时拒绝创建，要求模型带 extra_write_roots 重试。
def explicit_root_missing_write_root_error(params: dict[str, object], goal: str) -> str:
    role = str(params.get("role") or "worker").strip()
    if not is_explicit_root_role(role):
        return ""
    if _merged_extra_write_roots(params, goal):
        return ""
    if not _goal_needs_product_write_root(goal):
        return ""
    return (
        "显式 root/coordinator 要交付文件或网站时，必须提供真实产物写入根，"
        "否则下级会误把 agent-run workspace 当成 build 目录。"
        "请重新调用 create_subagents，并在顶层传入 extra_write_roots，"
        "例如 extra_write_roots=[\"/Users/.../deliverables/.../build\"]；"
        "不要只在 goal 里写“build 目录”。"
    )


# LLM: _goal_needs_product_write_root detects concrete deliverable tasks without parsing prose too broadly.
# 函数用途: 判断目标是否像文件/网站交付任务；只用于缺写入根时的保守拦截，不用于授权。
def _goal_needs_product_write_root(goal: str) -> bool:
    lowered = goal.lower()
    if not any(word in lowered for word in ("交付", "deliver", "build", "网站", "demo", "文件")):
        return False
    return any(suffix in lowered for suffix in (".html", ".css", ".js", ".py", ".md", ".json", ".txt"))


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
        status_filter = board_status_filter(params.get("status"))
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
                    "task_dir": item.task_dir,
                    "output_json": str(getattr(item, "output_json", "") or ""),
                }
                for item in items
            ],
            "board_json": str(self.agent.subagents.workspace / "subagent_board.json"),
            "board_md": str(self.agent.subagents.workspace / "SUBAGENT_BOARD.md"),
        }
        return ToolExecutionResult("subagent_board", True, json.dumps(payload, ensure_ascii=False, indent=2))
