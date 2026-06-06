
from __future__ import annotations

"""Dry-run batch leadership recovery planner for stale coordinator subtrees."""

import time
from dataclasses import dataclass, field
from typing import Any

from ...models import SubAgentDueCheckOptions, SubAgentLeadershipRecoveryPlanOptions, SubAgentTask


@dataclass(frozen=True)
class LeadershipRecoveryAssignment:
    coordinator_id: str
    leader_id: str
    child_ids: list[str]
    apply_supported: bool = False
    suggested_command: str = ""
    reason: str = ""


@dataclass(frozen=True)
class LeadershipRecoveryUnassigned:
    coordinator_id: str
    child_ids: list[str]
    reason: str


@dataclass(frozen=True)
class LeadershipRecoveryPlanReport:
    generated_at: float
    root_id: str
    max_children_per_leader: int
    summary: dict[str, int]
    assignments: list[LeadershipRecoveryAssignment] = field(default_factory=list)
    unassigned: list[LeadershipRecoveryUnassigned] = field(default_factory=list)
    candidate_leader_ids: list[str] = field(default_factory=list)


@dataclass
class _LeaderCapacity:
    task: SubAgentTask
    remaining: int


@dataclass(frozen=True)
class _SummaryInput:
    coordinators: list[SubAgentTask]
    leaders: list[_LeaderCapacity]
    assignments: list[LeadershipRecoveryAssignment]
    unassigned: list[LeadershipRecoveryUnassigned]
    failed_parent_count: int = 0


class SubAgentLeadershipRecoveryPlanner:
    """Plan batch coordinator leadership handoffs without mutating task records."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager

    def plan(self, request: SubAgentLeadershipRecoveryPlanOptions) -> LeadershipRecoveryPlanReport:
        stale_coordinators = self._stale_coordinators(request)
        failed_parents = self._failed_parent_sources(request, stale_coordinators)
        coordinators = _top_level_sources(self.manager, _unique_sources([*stale_coordinators, *failed_parents]))
        leaders = self._candidate_leaders(request)
        assignments, unassigned = _assign_coordinator_children(coordinators, leaders)
        return LeadershipRecoveryPlanReport(
            generated_at=time.time(),
            root_id=request.root_id,
            max_children_per_leader=max(0, int(request.max_children_per_leader or 0)),
            summary=_summary(
                _SummaryInput(
                    coordinators=coordinators,
                    leaders=leaders,
                    assignments=assignments,
                    unassigned=unassigned,
                    failed_parent_count=len(failed_parents),
                )
            ),
            assignments=assignments,
            unassigned=unassigned,
            candidate_leader_ids=[leader.task.id for leader in leaders],
        )

    def _stale_coordinators(self, request: SubAgentLeadershipRecoveryPlanOptions) -> list[SubAgentTask]:
        due_report = self.manager.board.due_check(
            params=SubAgentDueCheckOptions(config=request.config, root_id=request.root_id)
        )
        coordinators: list[SubAgentTask] = []
        for issue in due_report.issues:
            if issue.kind != "coordinator_heartbeat_stale":
                continue
            try:
                coordinator = self.manager.load(issue.run_id)
            except FileNotFoundError:
                continue
            if _is_scope_root(coordinator, request.root_id):
                continue
            coordinators.append(coordinator)
        return coordinators

    def _failed_parent_sources(
        self,
        request: SubAgentLeadershipRecoveryPlanOptions,
        stale_coordinators: list[SubAgentTask],
    ) -> list[SubAgentTask]:
        stale_ids = {task.id for task in stale_coordinators}
        failed: list[SubAgentTask] = []
        for task in self.manager.list_runs():
            if task.id in stale_ids or not task.child_ids:
                continue
            if not _leader_matches_scope(task, request.root_id):
                continue
            if _parent_is_failed_recovery_source(task):
                failed.append(task)
        return failed

    def _candidate_leaders(self, request: SubAgentLeadershipRecoveryPlanOptions) -> list[_LeaderCapacity]:
        leaders: list[_LeaderCapacity] = []
        max_children = int(request.max_children_per_leader or 0)
        for leader_id in request.leader_ids:
            try:
                leader = self.manager.load(leader_id)
            except FileNotFoundError:
                continue
            if not _leader_matches_scope(leader, request.root_id):
                continue
            if not _leader_is_available(leader):
                continue
            remaining = _remaining_capacity(leader, max_children)
            if remaining <= 0:
                continue
            leaders.append(_LeaderCapacity(task=leader, remaining=remaining))
        return leaders


def _assign_coordinator_children(
    coordinators: list[SubAgentTask],
    leaders: list[_LeaderCapacity],
) -> tuple[list[LeadershipRecoveryAssignment], list[LeadershipRecoveryUnassigned]]:
    assignments: list[LeadershipRecoveryAssignment] = []
    unassigned: list[LeadershipRecoveryUnassigned] = []
    leader_ids = {leader.task.id for leader in leaders}
    for coordinator in coordinators:
        pending = [child_id for child_id in coordinator.child_ids if child_id and child_id not in leader_ids]
        if not pending:
            continue
        pending = _assign_pending_children(coordinator, pending, leaders, assignments)
        if pending:
            unassigned.append(
                LeadershipRecoveryUnassigned(
                    coordinator_id=coordinator.id,
                    child_ids=pending,
                    reason="leader_capacity_exhausted" if leaders else "no_candidate_leaders",
                )
            )
    return assignments, unassigned


def _unique_sources(tasks: list[SubAgentTask]) -> list[SubAgentTask]:
    seen: set[str] = set()
    unique: list[SubAgentTask] = []
    for task in tasks:
        if task.id in seen:
            continue
        seen.add(task.id)
        unique.append(task)
    return unique


def _top_level_sources(manager: Any, tasks: list[SubAgentTask]) -> list[SubAgentTask]:
    selected_ids = {task.id for task in tasks}
    return [task for task in tasks if not _has_selected_ancestor(manager, task, selected_ids)]


def _has_selected_ancestor(manager: Any, task: SubAgentTask, selected_ids: set[str]) -> bool:
    parent_id = str(task.parent_id or "")
    seen: set[str] = set()
    while parent_id and parent_id not in seen:
        if parent_id in selected_ids:
            return True
        seen.add(parent_id)
        try:
            parent = manager.load(parent_id)
        except FileNotFoundError:
            return False
        parent_id = str(parent.parent_id or "")
    return False


def _assign_pending_children(
    coordinator: SubAgentTask,
    pending: list[str],
    leaders: list[_LeaderCapacity],
    assignments: list[LeadershipRecoveryAssignment],
) -> list[str]:
    for leader in leaders:
        if not pending:
            break
        if leader.remaining <= 0:
            continue
        batch = pending[: leader.remaining]
        pending = pending[leader.remaining :]
        leader.remaining -= len(batch)
        assignments.append(
            LeadershipRecoveryAssignment(
                coordinator_id=coordinator.id,
                leader_id=leader.task.id,
                child_ids=batch,
                suggested_command=_future_subset_apply_command(coordinator.id, leader.task.id, batch),
                reason="dry_run_split_handoff_requires_subset_apply",
            )
        )
    return pending


def _remaining_capacity(leader: SubAgentTask, max_children: int) -> int:
    if max_children <= 0:
        return 10**9
    return max(0, max_children - len(leader.child_ids))


def _leader_matches_scope(leader: SubAgentTask, root_id: str) -> bool:
    normalized = str(root_id or "").strip()
    return not normalized or (leader.root_id or leader.id) == normalized


def _is_scope_root(task: SubAgentTask, root_id: str) -> bool:
    normalized = str(root_id or "").strip()
    return bool(normalized) and task.id == normalized


def _leader_is_available(leader: SubAgentTask) -> bool:
    status = str(leader.status or "").upper()
    channel = str(leader.channel_status or "").upper()
    return status not in {"FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER", "ABANDONED"} and channel != "BROKEN"


def _parent_is_failed_recovery_source(task: SubAgentTask) -> bool:
    status = str(task.status or "").upper()
    channel = str(task.channel_status or "").upper()
    return status in {"FAILED", "TIMEOUT", "CHANNEL_ERROR", "ABANDONED"} or channel == "BROKEN"


def _future_subset_apply_command(coordinator_id: str, leader_id: str, child_ids: list[str]) -> str:
    child_flags = " ".join(f"--child-run-id {child_id}" for child_id in child_ids)
    return (
        "my-agent subagents-leadership-recovery-apply "
        f"--coordinator {coordinator_id} --leader {leader_id} {child_flags} --apply"
    )


def _summary(data: _SummaryInput) -> dict[str, int]:
    assigned_children = sum(len(item.child_ids) for item in data.assignments)
    unassigned_children = sum(len(item.child_ids) for item in data.unassigned)
    return {
        "stale_coordinators": len(data.coordinators) - data.failed_parent_count,
        "candidate_leaders": len(data.leaders),
        "assignments": len(data.assignments),
        "assigned_children": assigned_children,
        "unassigned_children": unassigned_children,
        "failed_parent_nodes": data.failed_parent_count,
    }
