
from __future__ import annotations

"""board and action planning service for subagent tasks.

这里承接看板构建、due-check 巡检、动作计划生成等逻辑。
SubAgentManager 通过 facade 方法委托到这里。
"""

import time
from typing import TYPE_CHECKING, Any

from ...models import SubAgentBoardOptions
from ...policies import _issue_weight, _risk_weight
from ...reports import ActionPlanReport, DueCheckReport, SubAgentBoard
from .action_plan import build_action_plan_report
from .due_summary import due_check_settings, due_check_summary, inspect_due_tasks
from .items import board_options as make_board_options
from .items import scoped_due_check_tasks, to_board_item

if TYPE_CHECKING:
    from ...capability_config import CapabilityConfig


class SubAgentBoardService:
    """Board, due-check, and action planning service."""

    def __init__(self, manager: Any):
        self.manager = manager

    def build_board(
        self,
        *,
        options: SubAgentBoardOptions | None = None,
        recent_limit: int = 20,
    ) -> SubAgentBoard:
        """Build the subagent traffic light board."""
        board_options = make_board_options(options, recent_limit=recent_limit)
        tasks = self.manager.list_runs()
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
        tasks = scoped_due_check_tasks(self.manager.list_runs(), root_id, include_run_ids, exclude_run_ids)
        issues = inspect_due_tasks(self.manager, tasks, settings)

        issues.sort(key=lambda issue: (-_issue_weight(issue), issue.run_id, issue.kind))
        report = DueCheckReport(generated_at=now, summary=due_check_summary(issues), issues=issues)
        from ...debug_trace_reports import trace_due_check_report

        return trace_due_check_report(self.manager, report)

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
