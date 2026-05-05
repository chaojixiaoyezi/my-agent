from __future__ import annotations

"""LLM: indexing and event logging service.

给人看的解释：
这里承接 LocalStore 写入、报告索引、任务索引等逻辑。
SubAgentManager 通过 facade 方法委托到这里。
"""

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import ChannelProbeResult, SubAgentExecutionContext, SubAgentTask
    from ..reports import (
        AcceptanceReviewRecord,
        ActionApplyRecord,
        CapabilityRouteRecord,
        DispatchRecord,
        DispatchWatchRecord,
        ParentPlannerRecord,
        PatchReviewRecord,
        SubAgentRunnerResult,
    )


class SubAgentIndexingService:
    """Indexing and local event logging service."""

    def __init__(self, manager: Any):
        self.manager = manager

    def log_local_record(
        self,
        *,
        source_type: str,
        source_id: str,
        title: str,
        content: str,
        metadata: dict[str, object] | None = None,
        event_type: str,
    ) -> None:
        """Write a subagent event to LocalStore; failures do not affect the file ledger."""
        if "_log_local_record" in self.manager.__dict__:
            self.manager._log_local_record(
                source_type=source_type,
                source_id=source_id,
                title=title,
                content=content,
                metadata=metadata or {},
                event_type=event_type,
            )
            return
        if not hasattr(self.manager, "local_store"):
            return
        if self.manager.local_store is None:
            return
        try:
            self.manager.local_store.log_record(
                source_type=source_type,
                source_id=source_id,
                title=title,
                content=content,
                metadata=metadata or {},
                event_type=event_type,
            )
        except Exception:
            return

    def index_task(self, task: SubAgentTask) -> None:
        """Index a task into the local store."""
        content = "\n".join(
            [
                "# Subagent Run",
                f"id: {task.id}",
                f"goal: {task.goal}",
                f"status: {task.status}",
                f"verification_status: {task.verification_status}",
                f"channel_status: {task.channel_status}",
                f"owner: {task.owner}",
                f"supervisor: {task.supervisor}",
                f"final_owner: {task.final_owner}",
                f"parent_id: {task.parent_id}",
                f"root_id: {task.root_id}",
                f"thought: {task.thought}",
                "plan:",
                *[f"- {item}" for item in task.plan],
                "acceptance_checks:",
                *[f"- {item}" for item in task.acceptance_checks],
                "evidence:",
                *[f"- [{item.kind}] {item.summary}" for item in task.evidence],
                "capability_requests:",
                *[f"- {item.id} {item.needed_capability} {item.problem} status={item.status}" for item in task.capability_requests],
                "capability_gaps:",
                *[f"- {item.id} {item.missing_capability} {item.why_failed} status={item.status}" for item in task.capability_gaps],
                f"task_dir: {task.task_dir}",
                f"output_json: {task.output_json}",
            ]
        )
        self.log_local_record(
            source_type="subagent_run",
            source_id=task.id,
            title=f"Subagent {task.id}: {task.goal}",
            content=content,
            metadata={
                "run_id": task.id,
                "goal": task.goal,
                "status": task.status,
                "verification_status": task.verification_status,
                "channel_status": task.channel_status,
                "root_id": task.root_id,
                "parent_id": task.parent_id,
                "depth": task.depth,
                "evidence_count": len(task.evidence),
                "capability_request_count": len(task.capability_requests),
                "capability_gap_count": len(task.capability_gaps),
                "task_dir": task.task_dir,
                "updated_at": task.updated_at,
            },
            event_type="subagent_run_saved",
        )

    def index_report(
        self,
        source_type: str,
        source_id: str,
        title: str,
        report: object,
        *,
        event_type: str,
    ) -> None:
        """Index a report into the local store."""
        try:
            payload = asdict(report)
        except TypeError:
            # not a dataclass - convert via __dict__ or use str representation
            payload = {"str": str(report), "repr": repr(report)}
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        self.log_local_record(
            source_type=source_type,
            source_id=source_id,
            title=title,
            content=content,
            metadata={
                "dry_run": bool(payload.get("dry_run", False)) if isinstance(payload, dict) else False,
                "generated_at": payload.get("generated_at", 0) if isinstance(payload, dict) else 0,
                "summary": payload.get("summary", {}) if isinstance(payload, dict) else {},
            },
            event_type=event_type,
        )

    def _index_dataclass_record(
        self,
        source_type: str,
        source_id: str,
        title: str,
        record: object,
        event_type: str,
    ) -> None:
        """Index a dataclass record."""
        try:
            payload = asdict(record)
        except TypeError:
            payload = {"str": str(record), "repr": repr(record)}
        metadata: dict[str, object] = {
            key: value
            for key, value in payload.items()
            if key in {
                "id", "run_id", "decision", "status", "ok",
                "applied", "dry_run", "created_at", "cycle", "backend", "tool_rounds",
            }
        }
        self.log_local_record(
            source_type=source_type,
            source_id=source_id,
            title=title,
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            metadata=metadata,
            event_type=event_type,
        )

    def index_action_apply(self, record: ActionApplyRecord) -> None:
        self._index_dataclass_record(
            "subagent_action_apply", record.id,
            f"Action apply {record.action} {record.run_id or 'global'}",
            record, "subagent_action_apply_logged",
        )

    def index_capability_route(self, record: CapabilityRouteRecord) -> None:
        self._index_dataclass_record(
            "subagent_capability_route", record.id,
            f"Capability route {record.request_id} {record.status}",
            record, "subagent_capability_route_logged",
        )

    def index_acceptance_review(self, record: AcceptanceReviewRecord) -> None:
        self._index_dataclass_record(
            "subagent_acceptance_review", record.id,
            f"Acceptance {record.decision} {record.run_id}",
            record, "subagent_acceptance_review_logged",
        )

    def index_patch_review(self, record: PatchReviewRecord) -> None:
        self._index_dataclass_record(
            "subagent_patch_review", record.id,
            f"Patch review {record.decision} {record.run_id}",
            record, "subagent_patch_review_logged",
        )

    def _index_via_manager_or_self(
        self,
        source_type: str,
        source_id: str,
        title: str,
        record: object,
        event_type: str,
    ) -> None:
        """Index a dataclass record via manager if available, otherwise via self."""
        if hasattr(self.manager, "_index_dataclass_record"):
            self.manager._index_dataclass_record(source_type, source_id, title, record, event_type)
        else:
            self._index_dataclass_record(source_type, source_id, title, record, event_type)

    def index_dispatch_record(self, record: DispatchRecord) -> None:
        self._index_via_manager_or_self(
            "subagent_dispatch", record.id,
            f"Dispatch {record.step}/{record.action} {record.run_id or 'global'}",
            record, "subagent_dispatch_logged",
        )

    def index_dispatch_watch_record(self, record: DispatchWatchRecord) -> None:
        self._index_via_manager_or_self(
            "subagent_dispatch_watch", record.id,
            f"Dispatch watch cycle {record.cycle}",
            record, "subagent_dispatch_watch_logged",
        )

    def index_parent_planner_record(self, record: ParentPlannerRecord) -> None:
        self._index_via_manager_or_self(
            "parent_planner", record.id,
            f"Parent planner {record.decision}",
            record, "parent_planner_logged",
        )

    def index_execution_context(self, context: SubAgentExecutionContext) -> None:
        self._index_via_manager_or_self(
            "subagent_execution_context", context.run_id,
            f"Execution context {context.run_id}",
            context, "subagent_execution_context_written",
        )

    def index_runner_result(self, result: SubAgentRunnerResult, output_payload: dict[str, object]) -> None:
        content = "\n".join(
            [
                json.dumps(asdict(result), ensure_ascii=False, indent=2),
                "",
                "## Output",
                json.dumps(output_payload, ensure_ascii=False, indent=2),
            ]
        )
        self.log_local_record(
            source_type="subagent_runner_result",
            source_id=f"{result.run_id}:{result.created_at:.6f}",
            title=f"Runner {result.run_id} {result.status}",
            content=content,
            metadata={
                "run_id": result.run_id,
                "dry_run": result.dry_run,
                "ok": result.ok,
                "status": result.status,
                "verification_status": result.verification_status,
                "backend": result.backend,
                "tool_rounds": result.tool_rounds,
                "result_json": result.result_json,
                "created_at": result.created_at,
            },
            event_type="subagent_runner_result_logged",
        )

    def index_channel_probe(self, result: ChannelProbeResult) -> None:
        self._index_dataclass_record(
            "subagent_channel_probe",
            f"{result.run_id}:{result.created_at:.6f}",
            f"Channel probe {result.run_id} {result.channel_status}",
            result, "subagent_channel_probe_logged",
        )

    def select_runs(self, run_ids: list[str] | None) -> list[SubAgentTask]:
        """Select runs by id, filtering out ineligible statuses."""
        from ..models import DISPATCH_INELIGIBLE_STATUSES

        if run_ids is None:
            all_runs = self.manager.list_runs()
            return [r for r in all_runs if r.status not in DISPATCH_INELIGIBLE_STATUSES]
        if not run_ids:
            return []
        runs: list[SubAgentTask] = []
        for run_id in run_ids:
            try:
                task = self.manager.load(run_id)
                if task.status not in DISPATCH_INELIGIBLE_STATUSES:
                    runs.append(task)
            except FileNotFoundError:
                continue
        return runs