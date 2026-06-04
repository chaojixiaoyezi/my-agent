from __future__ import annotations

"""Hierarchy service for child scheduling and recovery reports."""

import json
from dataclasses import asdict

from ...debug_trace_reports import trace_hierarchy_recovery_result
from ...models import SubAgentLeadershipRecoveryApplyOptions, SubAgentLeadershipRecoveryPlanOptions
from ..leadership_recovery import (
    LeadershipRecoveryApplyReport,
    LeadershipRecoveryPlanReport,
    SubAgentLeadershipRecoveryApplier,
    SubAgentLeadershipRecoveryPlanner,
)
from ..recovery.orchestrator import (
    RecoveryOrchestrationReport,
    RecoveryOrchestrationRequest,
    SubAgentRecoveryOrchestrator,
)
from .recovery import (
    HierarchyRecoveryRequest,
    HierarchyRecoveryResult,
    SubAgentHierarchyRecoveryService,
)
from .scheduler import HierarchyScheduleRequest, HierarchyScheduleResult, SubAgentHierarchyScheduler


class SubAgentHierarchyService:
    """Coordinates explicit hierarchy scheduling and leadership recovery."""

    def __init__(self, manager: object) -> None:
        self.manager = manager

    def schedule_child_runs(self, *, params: HierarchyScheduleRequest) -> HierarchyScheduleResult:
        return SubAgentHierarchyScheduler(self.manager).schedule_children(params)

    def build_hierarchy_recovery_packet(self, *, params: HierarchyRecoveryRequest) -> HierarchyRecoveryResult:
        result = SubAgentHierarchyRecoveryService(self.manager).build_packet(params)
        return trace_hierarchy_recovery_result(self.manager, result)

    def orchestrate_recovery(
        self,
        *,
        params: RecoveryOrchestrationRequest,
    ) -> RecoveryOrchestrationReport:
        return SubAgentRecoveryOrchestrator(self.manager).orchestrate(params)

    def plan_leadership_recovery(
        self,
        *,
        params: SubAgentLeadershipRecoveryPlanOptions,
    ) -> LeadershipRecoveryPlanReport:
        return SubAgentLeadershipRecoveryPlanner(self.manager).plan(params)

    def write_leadership_recovery_plan(
        self,
        *,
        params: SubAgentLeadershipRecoveryPlanOptions,
    ) -> LeadershipRecoveryPlanReport:
        report = self.plan_leadership_recovery(params=params)
        workspace = self.manager.workspace
        (workspace / "subagent_leadership_recovery_plan.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (workspace / "SUBAGENT_LEADERSHIP_RECOVERY_PLAN.md").write_text(
            render_leadership_recovery_plan(report),
            encoding="utf-8",
        )
        return report

    def apply_leadership_recovery(
        self,
        *,
        params: SubAgentLeadershipRecoveryApplyOptions,
    ) -> LeadershipRecoveryApplyReport:
        return SubAgentLeadershipRecoveryApplier(self.manager).apply(params)

    def write_leadership_recovery_apply(
        self,
        *,
        params: SubAgentLeadershipRecoveryApplyOptions,
    ) -> LeadershipRecoveryApplyReport:
        report = self.apply_leadership_recovery(params=params)
        workspace = self.manager.workspace
        (workspace / "subagent_leadership_recovery_apply_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (workspace / "SUBAGENT_LEADERSHIP_RECOVERY_APPLY.md").write_text(
            render_leadership_recovery_apply(report),
            encoding="utf-8",
        )
        return report


def render_leadership_recovery_plan(report: LeadershipRecoveryPlanReport) -> str:
    lines = [
        "# Subagent Leadership Recovery Plan",
        "",
        f"- root_id: `{report.root_id}`",
        f"- max_children_per_leader: `{report.max_children_per_leader}`",
        f"- summary: `{report.summary}`",
        "",
        "## Assignments",
    ]
    if not report.assignments:
        lines.append("- none")
    for item in report.assignments:
        lines.append(
            f"- coordinator `{item.coordinator_id}` -> leader `{item.leader_id}` "
            f"children={item.child_ids} apply_supported={item.apply_supported}"
        )
    lines.extend(["", "## Unassigned"])
    if not report.unassigned:
        lines.append("- none")
    for item in report.unassigned:
        lines.append(f"- coordinator `{item.coordinator_id}` children={item.child_ids} reason={item.reason}")
    return "\n".join(lines) + "\n"


def render_leadership_recovery_apply(report: LeadershipRecoveryApplyReport) -> str:
    lines = [
        "# Subagent Leadership Recovery Apply",
        "",
        f"- mode: `{'dry-run' if report.dry_run else 'apply'}`",
        f"- summary: `{report.summary}`",
        "",
        "## Records",
    ]
    if not report.records:
        lines.append("- none")
    for record in report.records:
        lines.append(
            f"- coordinator `{record.coordinator_id}` -> leader `{record.leader_id}` "
            f"ok={record.ok} applied={record.applied} moved={record.moved_child_ids}"
        )
        if record.blocked_by:
            lines.append(f"  - blocked_by: {record.blocked_by}")
        lines.append(f"  - message: {record.message}")
    return "\n".join(lines) + "\n"


__all__ = [
    "SubAgentHierarchyService",
    "render_leadership_recovery_apply",
    "render_leadership_recovery_plan",
]
