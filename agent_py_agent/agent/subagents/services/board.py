# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""board and action planning service for subagent tasks.

给人看的解释：
这里承接看板构建、due-check 巡检、动作计划生成等逻辑。
SubAgentManager 通过 facade 方法委托到这里。
"""

import time
from typing import TYPE_CHECKING, Any

from ..models import SubAgentBoardOptions
from ..policies import _issue_weight, _risk_weight
from ..reports import ActionPlanReport, DueCheckReport, SubAgentBoard
from .board_action_plan import build_action_plan_report
from .board_due_summary import due_check_settings, due_check_summary, inspect_due_tasks
from .board_items import board_options as make_board_options
from .board_items import scoped_due_check_tasks, to_board_item

if TYPE_CHECKING:
    from ..capability_config import CapabilityConfig


# LLM: SubAgentBoardService 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装subagent看板服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class SubAgentBoardService:
    """Board, due-check, and action planning service."""

    # LLM: __init__ 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def __init__(self, manager: Any):
        self.manager = manager

    # LLM: build_board 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建看板所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def build_board(
        self,
        *,
        options: SubAgentBoardOptions | None = None,
        recent_limit: int = 20,
    ) -> SubAgentBoard:
        """Build the subagent traffic light board."""
        board_options = make_board_options(options, recent_limit=recent_limit)
        tasks = self.manager.list_runs()
        # LLM: Full boards reuse the loaded task set for child counts; lightweight paths stay metadata-only.
        task_index = {task.id: task for task in tasks} if board_options.include_child_status_counts else None
        items = [
            to_board_item(
                self.manager,
                task,
                task_index=task_index,
                include_child_status_counts=board_options.include_child_status_counts,
            )
            for task in tasks
        ]
        summary: dict[str, int] = {"total": len(items)}
        for item in items:
            summary[item.status] = summary.get(item.status, 0) + 1
            summary[item.verification_status] = summary.get(item.verification_status, 0) + 1
            summary[f"channel_{item.channel_status}"] = summary.get(f"channel_{item.channel_status}", 0) + 1
        hot_list = [item for item in items if item.risk_flags]
        hot_list.sort(key=lambda item: (-_risk_weight(item.risk_flags), -(item.updated_at or 0)))
        recent = items[: board_options.recent_limit]
        return SubAgentBoard(
            generated_at=time.time(),
            summary=summary,
            hot_list=hot_list,
            recent=recent,
            items=items,
        )

    # LLM: due_check can scope inspection to one root task while preserving default all-runs behavior.
    # 函数用途: 巡检子代理是否超时、阻塞或缺证据；传 root_id 时只看该任务树。
    def due_check(
        self,
        config: CapabilityConfig | None = None,
        *,
        root_id: str = "",
        include_run_ids: list[str] | None = None,
        exclude_run_ids: list[str] | None = None,
    ) -> DueCheckReport:
        """Inspect all subagent runs to find issues needing parent intervention."""
        if config is None and hasattr(self.manager, "_make_default_capability_config"):
            config = self.manager._make_default_capability_config()
        now = time.time()
        settings = due_check_settings(config, now)
        # LLM: due-check uses one loaded task index so parent/child rules stay refs-only and cheap.
        tasks = scoped_due_check_tasks(self.manager.list_runs(), root_id, include_run_ids, exclude_run_ids)
        issues = inspect_due_tasks(self.manager, tasks, settings)

        issues.sort(key=lambda issue: (-_issue_weight(issue), issue.run_id, issue.kind))
        report = DueCheckReport(generated_at=now, summary=due_check_summary(issues), issues=issues)
        # LLM: due-check trace stores summary/kinds only so status views remain cheap.
        from ..debug_trace_reports import trace_due_check_report

        return trace_due_check_report(self.manager, report)

    # LLM: plan_actions reuses due-check scoping so dry-run actions stay tied to the active task tree.
    # 函数用途: 根据 due-check 问题生成 dry-run 动作计划；传 root_id 时只为这棵任务树生成建议。
    def plan_actions(
        self,
        config: CapabilityConfig | None = None,
        *,
        root_id: str = "",
        include_run_ids: list[str] | None = None,
        exclude_run_ids: list[str] | None = None,
    ) -> ActionPlanReport:
        """Convert due-check issues into a dry-run action plan."""
        due_report = self.due_check(
            config,
            root_id=root_id,
            include_run_ids=include_run_ids,
            exclude_run_ids=exclude_run_ids,
        )
        return build_action_plan_report(self.manager, due_report)
