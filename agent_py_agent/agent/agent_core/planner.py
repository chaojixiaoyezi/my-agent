# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""Compatibility wrappers for parent-planner state, prompts, and task projections."""

from typing import TYPE_CHECKING, Any

from ..capability_config import CapabilityConfig
from .planner_service import (
    build_parent_planner_prompt,
    build_parent_planner_state,
    combine_runner_instruction,
    task_state_for_planner,
)

if TYPE_CHECKING:
    from ..core import SimpleAgent

PARENT_PLANNER_READ_TOOLS = ["list_files", "read_file", "search_text"]
_combine_runner_instruction = combine_runner_instruction
_task_state_for_planner = task_state_for_planner


# LLM: _build_parent_planner_state 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建父级规划器状态所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _build_parent_planner_state(
    agent: SimpleAgent,
    cfg: CapabilityConfig | None = None,
    *,
    params: Any = None,
    max_runners: int = 1,
    limit: int = 20,
    reviewer: str = "",
    note: str = "",
) -> dict[str, object]:
    if params is not None:
        return build_parent_planner_state(agent, params=params)
    return build_parent_planner_state(
        agent,
        cfg or CapabilityConfig(),
        max_runners=max_runners,
        limit=limit,
        reviewer=reviewer,
        note=note,
    )


# LLM: _build_parent_planner_prompt 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建父级规划器提示词所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _build_parent_planner_prompt(
    state: dict[str, object],
    *,
    params: Any = None,
    apply: bool = False,
    execute_runners: bool = False,
    max_runners: int = 1,
    runner_instruction: str = "",
) -> str:
    if params is not None:
        return build_parent_planner_prompt(state, params=params)
    return build_parent_planner_prompt(
        state,
        apply=apply,
        execute_runners=execute_runners,
        max_runners=max_runners,
        runner_instruction=runner_instruction,
    )
