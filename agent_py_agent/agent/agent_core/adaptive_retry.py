from __future__ import annotations

"""LLM: adaptive retry logic for failed subagent tasks.

给人看的解释：
根据失败分析结果决定下一步：重试、拆分任务或停止。
"""

import time
from typing import TYPE_CHECKING

from ..subagents.models import SubAgentTask
from .failure_analyzer import FailureAnalysis

if TYPE_CHECKING:
    pass


def adaptive_retry(
    task: SubAgentTask,
    analysis: FailureAnalysis,
    max_split_depth: int = 2,
) -> SubAgentTask | list[SubAgentTask]:
    """根据失败分析结果决定下一步。

    Args:
        task: 失败的任务
        analysis: 失败分析结果
        max_split_depth: 最大拆分深度

    Returns:
        单个任务（重试）或任务列表（拆分），空列表表示停止
    """

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


def split_task(task: SubAgentTask, suggestions: list[str]) -> list[SubAgentTask]:
    """把大任务拆分成多个小任务。

    Args:
        task: 原始任务
        suggestions: 拆分建议

    Returns:
        拆分后的子任务列表
    """

    subtasks = [_build_split_subtask(task, suggestions, index, suggestion) for index, suggestion in enumerate(suggestions)]
    _mark_task_split(task, subtasks)

    return subtasks


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


def _split_subtask_plan(task: SubAgentTask, suggestions: list[str], index: int) -> list[str]:
    if task.plan and suggestions:
        step_per_subtask = max(1, len(task.plan) // len(suggestions))
        start_idx = index * step_per_subtask
        return task.plan[start_idx : start_idx + step_per_subtask]
    return task.plan if index == 0 else []


def _mark_task_split(task: SubAgentTask, subtasks: list[SubAgentTask]) -> None:
    task.status = "SPLIT"
    task.attributes["split_into"] = [st.id for st in subtasks]
    task.child_ids = [st.id for st in subtasks]
    task.updated_at = time.time()


def should_auto_split(task: SubAgentTask, max_depth: int = 2) -> bool:
    """判断是否应该自动拆分任务。"""

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


def estimate_split_count(task: SubAgentTask) -> int:
    """估算应该拆分成几个子任务。"""

    plan_length = len(task.plan)

    if plan_length <= 3:
        return 1

    if plan_length <= 6:
        return 2

    if plan_length <= 10:
        return 3

    return max(3, plan_length // 4)
