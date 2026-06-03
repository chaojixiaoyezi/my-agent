
from __future__ import annotations

"""action-plan construction helpers for subagent board service."""

import time
from dataclasses import dataclass
from typing import Any

from ...policies import _action_for_issue, _commands_for_action, _severity_weight
from ...reports import ActionPlanItem, ActionPlanReport, DueCheckIssue, DueCheckReport
from ...utils import _merge_list
from ..rescue_policy import merge_rescue_fields, rescue_fields_for_issue


@dataclass(frozen=True)
class NewActionPlanItemRequest:
    manager: Any
    issue: DueCheckIssue
    action: str
    priority: int
    would_change_status_to: str


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
