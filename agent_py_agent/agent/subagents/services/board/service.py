
from __future__ import annotations

"""board and action planning service for subagent tasks."""

import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from ...models import SubAgentBoardOptions, SubAgentDueCheckOptions, SubAgentPlanActionsOptions
from ...policies import (
    _action_for_issue,
    _commands_for_action,
    _filter_action_plan_items,
    _issue_weight,
    _risk_weight,
    _severity_weight,
    filter_board_items,
)
from ...rendering import (
    render_action_plan_markdown,
    render_board_markdown,
    render_due_check_markdown,
)
from ...reports import (
    ActionPlanItem,
    ActionPlanReport,
    DueCheckIssue,
    DueCheckReport,
    SubAgentBoard,
)
from ...utils import _merge_list
from ..rescue_policy import merge_rescue_fields, rescue_fields_for_issue
from .due_checks import DueCheckSettings, InspectTaskDueRequest, inspect_single_task_due
from .items import board_options as make_board_options
from .items import build_risk_flags, scoped_due_check_tasks, to_board_item

if TYPE_CHECKING:
    from agent_py_agent.agent.capability.config import CapabilityConfig


@dataclass(frozen=True)
class NewActionPlanItemRequest:
    manager: Any
    issue: DueCheckIssue
    action: str
    priority: int
    would_change_status_to: str


class SubAgentBoardService:
    """Board, due-check, and action planning service."""

    def __init__(self, manager: Any):
        self.manager = manager

    @property
    def _manager(self):
        return getattr(self, "manager", self)

    def to_board_item(self, task):
        return to_board_item(self._manager, task)

    def risk_flags(self, task, open_request_count=0, open_gap_count=0):
        return build_risk_flags(task, open_request_count, open_gap_count)

    def build_board(
        self,
        *,
        options: SubAgentBoardOptions | None = None,
        recent_limit: int = 20,
        candidate_ids: list[str] | None = None,
    ) -> SubAgentBoard:
        """Build the subagent traffic light board."""
        board_options = make_board_options(options, recent_limit=recent_limit)
        status = board_options.status.upper()
        tasks = _board_tasks(self._manager, candidate_ids, root_id=board_options.root_id, status=status)
        tasks = [task for task in tasks if (not status or task.status == status)
                 and (not board_options.owner or board_options.owner in (task.owner, task.supervisor, task.final_owner))
                 and (not board_options.root_id or (task.root_id or task.id) == board_options.root_id)]
        task_index = {task.id: task for task in tasks} if board_options.include_child_status_counts else None
        reader = getattr(self._manager, "list_runs_by_ids_report", None)
        if task_index and callable(reader):
            child_ids = {child_id for task in tasks for child_id in task.child_ids} - task_index.keys()
            if child_ids:
                report = reader(child_ids)
                errors = [error for error in report.load_errors if error.get("error_type") != "FileNotFoundError"]
                if errors:
                    from ..persistence.run_index import SubagentRunIndexUnavailable

                    error = SubagentRunIndexUnavailable("board child canonical unavailable")
                    error.load_errors = errors
                    raise error
                task_index.update((task.id, task) for task in report.runs)
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
        candidate_ids: list[str] | None = None,
    ) -> DueCheckReport:
        """Inspect all subagent runs to find issues needing parent intervention."""
        if params is not None:
            options = _due_check_options(config=config, params=params)
            config = options.config
            root_id = options.root_id
            include_run_ids = options.include_run_ids
            exclude_run_ids = options.exclude_run_ids
        now = time.time()
        settings = due_check_settings(config, now)
        tasks = _board_tasks(self._manager, candidate_ids, root_id=root_id, include_run_ids=include_run_ids)
        tasks = scoped_due_check_tasks(tasks, root_id, include_run_ids, exclude_run_ids)
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
        candidate_ids: list[str] | None = None,
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
            candidate_ids=candidate_ids,
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


def _board_tasks(manager, candidate_ids, *, root_id="", include_run_ids=None, status=""):
    from ...manager import SubAgentManager, request_scope_runs, select_current_runs
    from ..persistence.run_index import SubagentRunIndexUnavailable

    ids = candidate_ids if candidate_ids is not None else include_run_ids
    if ids == [] and candidate_ids is not None:
        return []
    if not isinstance(manager, SubAgentManager) or getattr(manager.list_runs, "__func__", None) is not SubAgentManager.list_runs:
        # 自定义列表适配器保留原协议；正式 manager 的索引未知绝不走此分支。
        allowed = set(ids) if ids else None
        return [task for task in manager.list_runs() if allowed is None or task.id in allowed]
    if ids is not None and (candidate_ids is not None or ids):
        report = manager.list_runs_by_ids_report(ids)
        if report.load_errors:
            error = SubagentRunIndexUnavailable("board canonical candidates unavailable")
            error.load_errors = report.load_errors
            raise error
        return report.runs
    if root_id:
        return request_scope_runs(manager, (root_id,), "", None)
    if status:
        return select_current_runs(manager, lambda task: task.status == status, statuses=(status,))
    # None 且没有范围是原来的显式历史诊断，空候选绝不走这里。
    return manager.list_runs()


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


# LLM: 巡检阈值只取自 CapabilityConfig 的三个在用字段；调用方没给配置时用 CapabilityConfig() 默认值，
#   这里不另写兜底数字（唯一权威是 dataclass 默认值）。改阈值来源须同步随包模板与 test_capability_config 的模板一致性测试。
# 函数用途: 把能力配置换算成一次巡检用的阈值快照，供 due-check 和行动计划共用。
def due_check_settings(config: Any, now: float) -> DueCheckSettings:
    from ....capability.config import CapabilityConfig

    config = config or CapabilityConfig()
    return DueCheckSettings(
        now=now,
        heartbeat_timeout=config.subagent_heartbeat_timeout,
        run_timeout=config.subagent_run_timeout,
        no_progress_attempt_limit=config.subagent_no_progress_attempt_limit,
    )


def inspect_due_tasks(manager: Any, tasks: list[Any], settings: DueCheckSettings) -> list[DueCheckIssue]:
    issues: list[DueCheckIssue] = []
    task_index = {task.id: task for task in tasks}
    for task in tasks:
        issues.extend(
            inspect_single_task_due(
                InspectTaskDueRequest(
                    manager=manager,
                    task=task,
                    settings=settings,
                    risk_flags_builder=build_risk_flags,
                    task_index=task_index,
                )
            )
        )
    return issues


def due_check_summary(issues: list[DueCheckIssue]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(issues)}
    for issue in issues:
        summary[issue.severity] = summary.get(issue.severity, 0) + 1
        summary[issue.kind] = summary.get(issue.kind, 0) + 1
    return summary


def build_action_plan_report(manager: Any, due_report: DueCheckReport) -> ActionPlanReport:
    merged: dict[tuple[str, str], ActionPlanItem] = {}
    for issue in due_report.issues:
        _merge_action_issue(manager, merged, issue)
    actions = list(merged.values())
    actions.sort(key=lambda item: (-item.priority, item.run_id, item.action))
    return action_plan_report(manager, actions)


def action_plan_report(manager: Any, actions: list[ActionPlanItem]) -> ActionPlanReport:
    summary: dict[str, int] = {"total": len(actions)}
    for action in actions:
        summary[action.severity] = summary.get(action.severity, 0) + 1
        summary[action.action] = summary.get(action.action, 0) + 1
    report = ActionPlanReport(generated_at=time.time(), summary=summary, actions=actions)
    from ...debug_trace_reports import trace_action_plan_report

    return trace_action_plan_report(manager, report)


def _merge_action_issue(
    manager: Any,
    merged: dict[tuple[str, str], ActionPlanItem],
    issue: DueCheckIssue,
) -> None:
    action, priority, would_change_status_to = _action_for_issue(issue)
    key = (issue.run_id, action)
    if key not in merged:
        merged[key] = _new_action_plan_item(
            NewActionPlanItemRequest(manager, issue, action, priority, would_change_status_to)
        )
        return
    _update_action_plan_item(merged[key], issue, action, priority)


def _new_action_plan_item(request: NewActionPlanItemRequest) -> ActionPlanItem:
    manager = request.manager
    issue = request.issue
    rescue_fields = rescue_fields_for_issue(issue, request.action)
    return ActionPlanItem(
        id=manager._new_id("action"),
        run_id=issue.run_id,
        severity=issue.severity,
        priority=request.priority,
        action=request.action,
        reason=issue.message,
        source_issue_kinds=[issue.kind],
        suggested_commands=_commands_for_action(request.action, issue.run_id),
        would_change_status_to=request.would_change_status_to,
        **rescue_fields,
        owner=issue.owner,
        final_owner=issue.final_owner,
        task_dir=issue.task_dir,
        created_at=time.time(),
    )


def _update_action_plan_item(
    item: ActionPlanItem,
    issue: DueCheckIssue,
    action: str,
    priority: int,
) -> None:
    item.source_issue_kinds = _merge_list(item.source_issue_kinds, [issue.kind])
    merge_rescue_fields(item, issue, action)
    item.reason = f"{item.reason} / {issue.message}"
    if _severity_weight(issue.severity) > _severity_weight(item.severity):
        item.severity = issue.severity
    item.priority = max(item.priority, priority)


__all__ = ["SubAgentBoardService"]
