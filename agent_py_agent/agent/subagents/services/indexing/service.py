
from __future__ import annotations

"""indexing and event logging service.

这里集中处理 LocalStore 写入、报告索引、任务索引等逻辑。
SubAgentManager 通过当前服务组合调用这里。
"""

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ....runtime_errors import runtime_error_report

if TYPE_CHECKING:
    from ...models import ChannelProbeResult, SubAgentExecutionContext, SubAgentTask
    from ...reports import (
        ActionApplyRecord,
        CapabilityRouteRecord,
        DispatchRecord,
        DispatchWatchRecord,
        ParentPlannerRecord,
        PatchReviewRecord,
        SubAgentRunnerResult,
    )

from .records import (
    DataclassRecordIndexParams,
    IndexReportParams,
    LocalRecordParams,
    index_action_apply_via,
    index_capability_route_via,
    index_channel_probe_via,
    index_dataclass_record_via,
    index_patch_review_via,
    index_runner_result_via,
)

_LOGGER = logging.getLogger(__name__)


class SubAgentIndexingService:
    """Indexing and local event logging service."""

    def __init__(self, manager: Any):
        self.manager = manager

    def log_local_record(
        self,
        *,
        params: LocalRecordParams,
    ) -> None:
        """Write a subagent event to LocalStore; failures do not affect the file ledger."""
        if not hasattr(self.manager, "local_store"):
            return
        if self.manager.local_store is None:
            return
        try:
            self.manager.local_store.log_record(
                source_type=params.source_type,
                source_id=params.source_id,
                title=params.title,
                content=params.content,
                metadata=params.metadata or {},
                event_type=params.event_type,
            )
        except Exception as exc:
            report = runtime_error_report(exc, context="subagent_index.local_store.log_record")
            report.update(
                {
                    "source_type": params.source_type,
                    "source_id": params.source_id,
                    "event_type": params.event_type,
                }
            )
            _LOGGER.warning("subagent local index write failed: %s", report)

    def index_task(self, task: SubAgentTask) -> None:
        """Index a task into the local store."""
        self.log_local_record(
            params=LocalRecordParams(
                source_type="subagent_run",
                source_id=task.id,
                title=f"Subagent {task.id}: {task.goal}",
                content=_task_index_content(task),
                metadata=_task_index_metadata(task),
                event_type="subagent_run_saved",
            ),
        )

    def index_report(
        self,
        params: IndexReportParams,
    ) -> None:
        """Index a report into the local store."""
        try:
            payload = asdict(params.report)
        except TypeError:
            # not a dataclass - convert via __dict__ or use str representation
            payload = {"str": str(params.report), "repr": repr(params.report)}
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        self.log_local_record(
            params=LocalRecordParams(
                source_type=params.source_type,
                source_id=params.source_id,
                title=params.title,
                content=content,
                metadata={
                    "dry_run": bool(payload.get("dry_run", False)) if isinstance(payload, dict) else False,
                    "generated_at": payload.get("generated_at", 0) if isinstance(payload, dict) else 0,
                    "summary": payload.get("summary", {}) if isinstance(payload, dict) else {},
                },
                event_type=params.event_type,
            ),
        )

    def _index_dataclass_record(
        self,
        params: DataclassRecordIndexParams,
    ) -> None:
        """Index a dataclass record."""
        index_dataclass_record_via(self, params)

    def index_action_apply(self, record: ActionApplyRecord) -> None:
        index_action_apply_via(self, record)

    def index_capability_route(self, record: CapabilityRouteRecord) -> None:
        index_capability_route_via(self, record)

    def index_patch_review(self, record: PatchReviewRecord) -> None:
        index_patch_review_via(self, record)

    def index_dispatch_record(self, record: DispatchRecord) -> None:
        _index_dispatch_record_via(self, record)

    def index_dispatch_watch_record(self, record: DispatchWatchRecord) -> None:
        _index_dispatch_watch_record_via(self, record)

    def index_parent_planner_record(self, record: ParentPlannerRecord) -> None:
        _index_parent_planner_record_via(self, record)

    def index_execution_context(self, context: SubAgentExecutionContext) -> None:
        _index_execution_context_via(self, context)

    def index_runner_result(self, result: SubAgentRunnerResult, output_payload: dict[str, object]) -> None:
        index_runner_result_via(self, result, output_payload)

    def index_channel_probe(self, result: ChannelProbeResult) -> None:
        index_channel_probe_via(self, result)

    def select_runs(self, run_ids: list[str] | None) -> list[SubAgentTask]:
        """Select runs by id, filtering out ineligible statuses."""
        from ...models import DISPATCH_INELIGIBLE_STATUSES
        if run_ids is None:
            all_runs = self.manager.list_runs()
            return [r for r in all_runs if r.status not in DISPATCH_INELIGIBLE_STATUSES]
        if not run_ids:
            return []
        runs: list[SubAgentTask] = []
        for run_id in run_ids:
            task = _load_eligible_run(self.manager, run_id, DISPATCH_INELIGIBLE_STATUSES)
            if task is not None:
                runs.append(task)
        return runs


def _task_index_content(task: SubAgentTask) -> str:
    content_lines = _task_index_header_lines(task)
    content_lines.extend(_task_index_list_section("plan", task.plan))
    content_lines.extend(_task_index_list_section("acceptance_checks", task.acceptance_checks))
    content_lines.extend(["evidence:"])
    content_lines.extend(f"- [{item.kind}] {item.summary}" for item in task.evidence)
    content_lines.extend(["capability_requests:"])
    content_lines.extend(
        f"- {item.id} {item.needed_capability} {item.problem} status={item.status}"
        for item in task.capability_requests
    )
    content_lines.extend(["capability_gaps:"])
    content_lines.extend(
        f"- {item.id} {item.missing_capability} {item.why_failed} status={item.status}"
        for item in task.capability_gaps
    )
    content_lines.append(f"task_dir: {task.task_dir}")
    content_lines.append(f"output_json: {task.output_json}")
    return "\n".join(content_lines)


def _load_eligible_run(
    manager: Any,
    run_id: str,
    ineligible_statuses: set[str],
) -> SubAgentTask | None:
    try:
        task = manager.load(run_id)
    except FileNotFoundError:
        return None
    return None if task.status in ineligible_statuses else task


def _index_dispatch_record_via(
    service: SubAgentIndexingService,
    record: DispatchRecord,
) -> None:
    title = f"Dispatch {record.step}/{record.action} {record.run_id or 'global'}"
    service._index_dataclass_record(
        DataclassRecordIndexParams(
            "subagent_dispatch", record.id, title, record, "subagent_dispatch_logged",
        ),
    )


def _index_dispatch_watch_record_via(
    service: SubAgentIndexingService,
    record: DispatchWatchRecord,
) -> None:
    title = f"Dispatch watch cycle {record.cycle}"
    service._index_dataclass_record(
        DataclassRecordIndexParams(
            "subagent_dispatch_watch", record.id, title, record, "subagent_dispatch_watch_logged",
        ),
    )


def _index_parent_planner_record_via(
    service: SubAgentIndexingService,
    record: ParentPlannerRecord,
) -> None:
    title = f"Parent planner {record.decision}"
    service._index_dataclass_record(
        DataclassRecordIndexParams(
            "parent_planner", record.id, title, record, "parent_planner_logged",
        ),
    )


def _index_execution_context_via(
    service: SubAgentIndexingService,
    context: SubAgentExecutionContext,
) -> None:
    title = f"Execution context {context.run_id}"
    service._index_dataclass_record(
        DataclassRecordIndexParams(
            "subagent_execution_context", context.run_id, title, context, "subagent_execution_context_written",
        ),
    )


def _task_index_header_lines(task: SubAgentTask) -> list[str]:
    return [
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
    ]


def _task_index_list_section(name: str, values: list[str]) -> list[str]:
    return [f"{name}:", *(f"- {item}" for item in values)]


def _task_index_metadata(task: SubAgentTask) -> dict[str, object]:
    return {
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
    }
