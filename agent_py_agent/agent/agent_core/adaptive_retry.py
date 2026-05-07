# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""adaptive retry logic for failed subagent tasks.

根据失败分析结果决定下一步：重试、拆分任务或停止。
"""

import time
from typing import TYPE_CHECKING

from ..subagents.models import SubAgentTask
from .failure_analyzer import FailureAnalysis

if TYPE_CHECKING:
    pass


# LLM: adaptive_retry 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理adaptiveretry相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def adaptive_retry(
    task: SubAgentTask,
    analysis: FailureAnalysis,
    max_split_depth: int = 2,
) -> SubAgentTask | list[SubAgentTask]:

    # 不应该重试的情况
    if not analysis.should_retry and not analysis.should_split:
        return []

    # 需要拆分的情况
    if analysis.should_split:
        # 检查拆分深度
        current_depth = task.depth
        if current_depth >= max_split_depth:
            # 超过最大深度，停止拆分
            return []

        return split_task(task, analysis.split_suggestions)

    # 需要调整超时的情况
    if analysis.should_adjust_timeout and analysis.new_timeout_seconds:
        # 更新任务的超时配置
        task.attributes["dynamic_timeout_seconds"] = analysis.new_timeout_seconds
        task.runner_attempts = 0
        task.status = "PLANNING"
        task.failure_type = ""
        return [task]

    # 普通重试
    task.runner_attempts = 0
    task.status = "PLANNING"
    task.failure_type = ""
    return [task]


# LLM: split_task 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 拆分任务输入集合，给调度、验收或补丁处理提供分组结果；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def split_task(task: SubAgentTask, suggestions: list[str]) -> list[SubAgentTask]:

    subtasks = [_build_split_subtask(task, suggestions, index, suggestion) for index, suggestion in enumerate(suggestions)]
    _mark_task_split(task, subtasks)

    return subtasks


# LLM: _build_split_subtask 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 构建subtask所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _build_split_subtask(
    task: SubAgentTask,
    suggestions: list[str],
    index: int,
    suggestion: str,
) -> SubAgentTask:
    subtask = SubAgentTask(
        id=f"{task.id}-part-{index+1:02d}",
        goal=f"{task.goal} - 第{index+1}部分：{suggestion}",
        thought=task.thought,
        plan=_split_subtask_plan(task, suggestions, index),
        agent_name=task.agent_name,
        role=task.role,
        owner=task.owner,
        supervisor=task.supervisor,
        parent_id=task.id,
        root_id=task.root_id or task.id,
        depth=task.depth + 1,
        allowed_skills=list(task.allowed_skills),
        allowed_tools=list(task.allowed_tools),
        workflow_mode=task.workflow_mode,
        created_at=time.time(),
        updated_at=time.time(),
    )
    if task.context_manifest:
        subtask.context_manifest = task.context_manifest
    return subtask


# LLM: _split_subtask_plan 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 拆分subtask计划输入集合，给调度、验收或补丁处理提供分组结果；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _split_subtask_plan(task: SubAgentTask, suggestions: list[str], index: int) -> list[str]:
    if task.plan and suggestions:
        step_per_subtask = max(1, len(task.plan) // len(suggestions))
        start_idx = index * step_per_subtask
        return task.plan[start_idx : start_idx + step_per_subtask]
    return task.plan if index == 0 else []


# LLM: _mark_task_split 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 更新任务split对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新运行循环、工具调用、调度记录和最终响应，需避免破坏既有状态机约定。
def _mark_task_split(task: SubAgentTask, subtasks: list[SubAgentTask]) -> None:
    task.status = "SPLIT"
    task.attributes["split_into"] = [st.id for st in subtasks]
    task.child_ids = [st.id for st in subtasks]
    task.updated_at = time.time()


# LLM: should_auto_split 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 判断autosplit条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def should_auto_split(task: SubAgentTask, max_depth: int = 2) -> bool:

    # 检查深度限制
    if task.depth >= max_depth:
        return False

    # 检查任务复杂度
    if len(task.plan) <= 3:
        return False

    # 检查失败类型
    if task.failure_type == "runner_timeout" and task.runner_attempts >= 2:
        return True

    return False


# LLM: estimate_split_count 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 计算数量的预算、数量或限制，影响后续调度节奏；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def estimate_split_count(task: SubAgentTask) -> int:

    plan_length = len(task.plan)

    if plan_length <= 3:
        return 1

    if plan_length <= 6:
        return 2

    if plan_length <= 10:
        return 3

    return max(3, plan_length // 4)
