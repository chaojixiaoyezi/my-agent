
from __future__ import annotations

"""board and action planning service for subagent tasks."""

import json
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from ...models import SubAgentBoardOptions, SubAgentDueCheckOptions, SubAgentPlanActionsOptions
from ...policies import _filter_action_plan_items, _issue_weight, _risk_weight, filter_board_items
from ...rendering import (
    render_action_plan_markdown,
    render_board_markdown,
    render_due_check_markdown,
)
from ...reports import ActionPlanReport, DueCheckReport, SubAgentBoard
from .action_plan import build_action_plan_report
from .due_summary import due_check_settings, due_check_summary, inspect_due_tasks
from .items import board_options as make_board_options
from .items import build_risk_flags, scoped_due_check_tasks, to_board_item

if TYPE_CHECKING:
    from agent_py_agent.agent.capability.config import CapabilityConfig


class SubAgentBoardService:
    """Board, due-check, and action planning service."""

    def __init__(self, manager: Any):
        self.manager = manager

    @property
    def _manager(self):
        return getattr(self, "manager", self)

    def _to_board_item(self, task):
        return self.to_board_item(task)

    def to_board_item(self, task):
        return to_board_item(self._manager, task)

    def _risk_flags(self, task, open_request_count=0, open_gap_count=0):
        return self.risk_flags(task, open_request_count, open_gap_count)

    def risk_flags(self, task, open_request_count=0, open_gap_count=0):
        return build_risk_flags(task, open_request_count, open_gap_count)

    def build_board(
        self,
        *,
        options: SubAgentBoardOptions | None = None,
        recent_limit: int = 20,
    ) -> SubAgentBoard:
        """Build the subagent traffic light board."""
        board_options = make_board_options(options, recent_limit=recent_limit)
        tasks = self._manager.list_runs()
        task_index = {task.id: task for task in tasks} if board_options.include_child_status_counts else None
        items = [
            to_board_item(
                self._manager,
                task,
                task_index=task_index,
                include_child_status_counts=board_options.include_child_status_counts,
            )
            for task in tasks
        ]
        items = filter_board_items(
            items,
            status=board_options.status,
            owner=board_options.owner,
            root_id=board_options.root_id,
        )
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

    def write_board(
        self,
        *,
        options: SubAgentBoardOptions | None = None,
        recent_limit: int = 20,
    ) -> SubAgentBoard:
        board_options = _board_options(options, recent_limit=recent_limit)
        board = self.build_board(options=board_options)
        (self._manager.workspace / "subagent_board.json").write_text(
            json.dumps(asdict(board), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self._manager.workspace / "SUBAGENT_BOARD.md").write_text(
            render_board_markdown(board),
            encoding="utf-8",
        )
        return board

    def due_check(
        self,
        config: CapabilityConfig | None = None,
        *,
        params: SubAgentDueCheckOptions | None = None,
        root_id: str = "",
        include_run_ids: list[str] | None = None,
        exclude_run_ids: list[str] | None = None,
    ) -> DueCheckReport:
        """Inspect all subagent runs to find issues needing parent intervention."""
        if params is not None:
            options = _due_check_options(config=config, params=params)
            config = options.config
            root_id = options.root_id
            include_run_ids = options.include_run_ids
            exclude_run_ids = options.exclude_run_ids
        if config is None and hasattr(self._manager, "_make_default_capability_config"):
            config = self._manager._make_default_capability_config()
        now = time.time()
        settings = due_check_settings(config, now)
        tasks = scoped_due_check_tasks(self._manager.list_runs(), root_id, include_run_ids, exclude_run_ids)
        issues = inspect_due_tasks(self._manager, tasks, settings)

        issues.sort(key=lambda issue: (-_issue_weight(issue), issue.run_id, issue.kind))
        report = DueCheckReport(generated_at=now, summary=due_check_summary(issues), issues=issues)
        from ...debug_trace_reports import trace_due_check_report

        return trace_due_check_report(self._manager, report)

    def due_check_from_options(self, config=None, *, params: SubAgentDueCheckOptions | None = None):
        return self.due_check(config, params=params)

    def write_due_check(self, config=None, *, params: SubAgentDueCheckOptions | None = None):
        report = self.due_check_from_options(config=config, params=params)
        (self._manager.workspace / "subagent_due_check.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self._manager.workspace / "SUBAGENT_DUE_CHECK.md").write_text(
            render_due_check_markdown(report),
            encoding="utf-8",
        )
        return report

    def plan_actions(
        self,
        config: CapabilityConfig | None = None,
        *,
        params: SubAgentPlanActionsOptions | None = None,
        root_id: str = "",
        include_run_ids: list[str] | None = None,
        exclude_run_ids: list[str] | None = None,
    ) -> ActionPlanReport:
        """Convert due-check issues into a dry-run action plan."""
        if params is not None:
            options = _plan_actions_options(config=config, params=params)
            config = options.config
            root_id = options.root_id
            include_run_ids = options.include_run_ids
            exclude_run_ids = options.exclude_run_ids
        due_report = self.due_check(
            config,
            root_id=root_id,
            include_run_ids=include_run_ids,
            exclude_run_ids=exclude_run_ids,
        )
        return build_action_plan_report(self._manager, due_report)

    def plan_actions_from_options(self, config=None, *, params: SubAgentPlanActionsOptions | None = None):
        return self.plan_actions(config, params=params)

    def write_action_plan(self, config=None, *, params: SubAgentPlanActionsOptions | None = None):
        report = self.plan_actions_from_options(config=config, params=params)
        (self._manager.workspace / "subagent_action_plan.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self._manager.workspace / "SUBAGENT_ACTION_PLAN.md").write_text(
            render_action_plan_markdown(report),
            encoding="utf-8",
        )
        return report

    def filter_action_plan_items(self, actions, action_filter="", run_id="", limit=0):
        return _filter_action_plan_items(actions, action_filter=action_filter, run_id=run_id, limit=limit)

    def _filter_action_plan_items(self, actions, action_filter="", run_id="", limit=0):
        return self.filter_action_plan_items(
            actions,
            action_filter=action_filter,
            run_id=run_id,
            limit=limit,
        )


def _due_check_options(
    *,
    config,
    params: SubAgentDueCheckOptions | None,
) -> SubAgentDueCheckOptions:
    if params is not None:
        if not isinstance(params, SubAgentDueCheckOptions):
            raise TypeError("due check requires params: SubAgentDueCheckOptions")
        return params
    return SubAgentDueCheckOptions(config=config)


def _plan_actions_options(
    *,
    config,
    params: SubAgentPlanActionsOptions | None,
) -> SubAgentPlanActionsOptions:
    if params is not None:
        if not isinstance(params, SubAgentPlanActionsOptions):
            raise TypeError("plan actions requires params: SubAgentPlanActionsOptions")
        return params
    return SubAgentPlanActionsOptions(config=config)


def _board_options(
    options: SubAgentBoardOptions | None,
    *,
    recent_limit: int,
) -> SubAgentBoardOptions:
    if options is not None:
        if not isinstance(options, SubAgentBoardOptions):
            raise TypeError("board requires options: SubAgentBoardOptions")
        return options
    return SubAgentBoardOptions(recent_limit=recent_limit)


__all__ = ["SubAgentBoardService"]
