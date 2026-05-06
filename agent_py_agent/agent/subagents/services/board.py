from __future__ import annotations

"""LLM: board and action planning service for subagent tasks.

给人看的解释：
这里承接看板构建、due-check 巡检、动作计划生成等逻辑。
SubAgentManager 通过 facade 方法委托到这里。
"""

import time
from typing import TYPE_CHECKING, Any

from ..models import SubAgentTask
from ..policies import (
    _action_for_issue,
    _commands_for_action,
    _issue_weight,
    _risk_weight,
    _severity_weight,
)
from ..reports import (
    ActionPlanItem,
    ActionPlanReport,
    DueCheckIssue,
    DueCheckReport,
    SubAgentBoard,
    SubAgentBoardItem,
)
from ..utils import _merge_list
from .board_due_checks import DueCheckSettings, inspect_single_task_due

if TYPE_CHECKING:
    from ..capability_config import CapabilityConfig


def _build_risk_flags(
    task: SubAgentTask,
    open_request_count: int,
    open_gap_count: int,
) -> list[str]:
    """Calculate risk flags for a task."""
    flags: list[str] = []
    if task.status in {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        flags.append(task.status.lower())
    if task.status == "DONE" and not task.evidence:
        flags.append("done_without_evidence")
    if task.status == "DONE" and task.verification_status != "VERIFIED":
        flags.append("done_without_verification")
    if open_request_count:
        flags.append("open_capability_request")
    if open_gap_count:
        flags.append("open_capability_gap")
    if task.takeover_by:
        flags.append("taken_over")
    if task.channel_status == "BROKEN":
        flags.append("channel_broken")
    if task.channel_status == "DEGRADED":
        flags.append("channel_degraded")
    return flags


def _to_board_item(
    manager: Any,
    task: SubAgentTask,
) -> SubAgentBoardItem:
    """Convert a task to a board item."""
    open_request_count = sum(1 for item in task.capability_requests if item.status == "OPEN")
    open_gap_count = sum(1 for item in task.capability_gaps if item.status == "OPEN")
    flags = _build_risk_flags(task, open_request_count, open_gap_count)
    # LLM: child status counts let parents inspect the task tree without reading every work log.
    child_status_counts = _child_status_counts(manager, task)
    return SubAgentBoardItem(
        id=task.id,
        root_id=task.root_id,
        parent_id=task.parent_id,
        depth=task.depth,
        status=task.status,
        verification_status=task.verification_status,
        channel_status=task.channel_status,
        owner=task.owner,
        supervisor=task.supervisor,
        final_owner=task.final_owner,
        goal=task.goal,
        updated_at=task.updated_at,
        heartbeat_at=task.heartbeat_at,
        evidence_count=len(task.evidence),
        evidence_packet_count=len(task.evidence_packets),
        finding_count=len(task.findings),
        open_request_count=open_request_count,
        open_gap_count=open_gap_count,
        child_count=len(task.child_ids),
        child_status_counts=child_status_counts,
        progress=max(0.0, min(1.0, float(task.progress or 0.0))),
        current_step=task.current_step,
        latest_summary=task.latest_summary,
        blocker_count=len(task.blockers),
        takeover_by=task.takeover_by,
        locked_file_count=len(task.locked_files),
        risk_flags=flags,
        task_dir=task.task_dir,
        output_json=task.output_json,
    )


def _child_status_counts(manager: Any, task: SubAgentTask) -> dict[str, int]:
    counts: dict[str, int] = {}
    for child_id in task.child_ids:
        try:
            child = manager.load(child_id)
        except (FileNotFoundError, TypeError):
            counts["missing"] = counts.get("missing", 0) + 1
            continue
        counts[child.status] = counts.get(child.status, 0) + 1
    return counts


class SubAgentBoardService:
    """Board, due-check, and action planning service."""

    def __init__(self, manager: Any):
        self.manager = manager

    def build_board(self, *, recent_limit: int = 20) -> SubAgentBoard:
        """Build the subagent traffic light board."""
        items = [_to_board_item(self.manager, task) for task in self.manager.list_runs()]
        summary: dict[str, int] = {"total": len(items)}
        for item in items:
            summary[item.status] = summary.get(item.status, 0) + 1
            summary[item.verification_status] = summary.get(item.verification_status, 0) + 1
            summary[f"channel_{item.channel_status}"] = summary.get(f"channel_{item.channel_status}", 0) + 1
        hot_list = [item for item in items if item.risk_flags]
        hot_list.sort(key=lambda item: (-_risk_weight(item.risk_flags), -(item.updated_at or 0)))
        recent = items[:recent_limit]
        return SubAgentBoard(
            generated_at=time.time(),
            summary=summary,
            hot_list=hot_list,
            recent=recent,
            items=items,
        )

    def due_check(self, config: CapabilityConfig | None = None) -> DueCheckReport:
        """Inspect all subagent runs to find issues needing parent intervention."""
        if config is None and hasattr(self.manager, "_make_default_capability_config"):
            config = self.manager._make_default_capability_config()
        cfg = config
        now = time.time()
        heartbeat_timeout = cfg.subagent_heartbeat_timeout if cfg else 0
        run_timeout = cfg.subagent_run_timeout if cfg else 0
        min_evidence = cfg.subagent_min_evidence_for_done if cfg else 0
        settings = DueCheckSettings(now, heartbeat_timeout, run_timeout, min_evidence)
        issues: list[DueCheckIssue] = []

        for task in self.manager.list_runs():
            issues.extend(
                inspect_single_task_due(
                    self.manager,
                    task,
                    settings,
                    risk_flags_builder=_build_risk_flags,
                )
            )

        issues.sort(key=lambda issue: (-_issue_weight(issue), issue.run_id, issue.kind))
        summary: dict[str, int] = {"total": len(issues)}
        for issue in issues:
            summary[issue.severity] = summary.get(issue.severity, 0) + 1
            summary[issue.kind] = summary.get(issue.kind, 0) + 1
        return DueCheckReport(generated_at=now, summary=summary, issues=issues)

    def plan_actions(self, config: CapabilityConfig | None = None) -> ActionPlanReport:
        """Convert due-check issues into a dry-run action plan."""
        due_report = self.due_check(config)
        merged: dict[tuple[str, str], ActionPlanItem] = {}
        for issue in due_report.issues:
            action, priority, would_change_status_to = _action_for_issue(issue)
            key = (issue.run_id, action)
            if key not in merged:
                merged[key] = ActionPlanItem(
                    id=self.manager._new_id("action"),
                    run_id=issue.run_id,
                    severity=issue.severity,
                    priority=priority,
                    action=action,
                    reason=issue.message,
                    source_issue_kinds=[issue.kind],
                    suggested_commands=_commands_for_action(action, issue.run_id),
                    would_change_status_to=would_change_status_to,
                    owner=issue.owner,
                    final_owner=issue.final_owner,
                    task_dir=issue.task_dir,
                    created_at=time.time(),
                )
                continue
            item = merged[key]
            item.source_issue_kinds = _merge_list(item.source_issue_kinds, [issue.kind])
            item.reason = f"{item.reason} / {issue.message}"
            if _severity_weight(issue.severity) > _severity_weight(item.severity):
                item.severity = issue.severity
            item.priority = max(item.priority, priority)

        actions = list(merged.values())
        actions.sort(key=lambda item: (-item.priority, item.run_id, item.action))
        summary: dict[str, int] = {"total": len(actions)}
        for action in actions:
            summary[action.severity] = summary.get(action.severity, 0) + 1
            summary[action.action] = summary.get(action.action, 0) + 1
        return ActionPlanReport(generated_at=time.time(), summary=summary, actions=actions)
