from __future__ import annotations

"""LLM contract: SubAgentIndexingMixin methods grouped by one subagent responsibility.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
拆成 mixin 是为了让每个文件只有一个变化原因，而不是把所有父代理逻辑塞进一个巨型文件。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from .models import (
    DISPATCH_INELIGIBLE_STATUSES,
    ChannelProbeResult,
    SubAgentExecutionContext,
    SubAgentRunnerResult,
    SubAgentTask,
)
from .reports import (
    AcceptanceReviewRecord,
    ActionApplyRecord,
    CapabilityRouteRecord,
    DispatchRecord,
    DispatchWatchRecord,
    ParentPlannerRecord,
    PatchReviewRecord,
)
from .runner_rendering import _render_runner_item_line
from .parsing import (
    _dict_list,
    _normalize_runner_items,
    _split_allowed_items,
    _string_dict,
    _string_list,
)
from .policies import (
    _action_for_issue,
    _capability_request_query,
    _commands_for_action,
    _dedupe_granted_cards,
    _default_forbidden_write_roots,
    _execution_context_instructions,
    _filter_action_plan_items,
    _is_active,
    _issue_weight,
    _make_due_issue,
    _risk_weight,
    _route_card_payload,
    _severity_weight,
    _runner_next_action,
    _select_capability_hits,
    _status_from_structured_output,
    _verification_from_runner_status,
)
from .probe import (
    _channel_status,
    _probe_fail,
    _probe_json_file,
    _probe_ok,
    _probe_writable_dir,
)
from .utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _read_json_object,
    _write_if_missing,
    _write_json_if_missing,
)
from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl

if TYPE_CHECKING:
    from ..local_store import LocalStore

class SubAgentIndexingMixin:
    def _log_local_record(
        self,
        *,
        source_type: str,
        source_id: str,
        title: str,
        content: str,
        metadata: dict[str, object] | None = None,
        event_type: str,
    ) -> None:
        """把 subagent 侧事件写入 LocalStore；失败不影响原文件账本。"""

        if not self.local_store:
            return
        try:
            self.local_store.log_record(
                source_type=source_type,
                source_id=source_id,
                title=title,
                content=content,
                metadata=metadata or {},
                event_type=event_type,
            )
        except Exception:
            return

    def _index_task(self, task: SubAgentTask) -> None:
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
        self._log_local_record(
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

    def _index_report(
        self,
        source_type: str,
        source_id: str,
        title: str,
        report: object,
        *,
        event_type: str,
    ) -> None:
        payload = asdict(report)
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        self._log_local_record(
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

    def _index_action_apply(self, record: ActionApplyRecord) -> None:
        self._index_dataclass_record(
            "subagent_action_apply",
            record.id,
            f"Action apply {record.action} {record.run_id or 'global'}",
            record,
            "subagent_action_apply_logged",
        )

    def _index_capability_route(self, record: CapabilityRouteRecord) -> None:
        self._index_dataclass_record(
            "subagent_capability_route",
            record.id,
            f"Capability route {record.request_id} {record.status}",
            record,
            "subagent_capability_route_logged",
        )

    def _index_acceptance_review(self, record: AcceptanceReviewRecord) -> None:
        self._index_dataclass_record(
            "subagent_acceptance_review",
            record.id,
            f"Acceptance {record.decision} {record.run_id}",
            record,
            "subagent_acceptance_review_logged",
        )

    def _index_patch_review(self, record: PatchReviewRecord) -> None:
        self._index_dataclass_record(
            "subagent_patch_review",
            record.id,
            f"Patch review {record.decision} {record.run_id}",
            record,
            "subagent_patch_review_logged",
        )

    def _index_dispatch_record(self, record: DispatchRecord) -> None:
        self._index_dataclass_record(
            "subagent_dispatch",
            record.id,
            f"Dispatch {record.step}/{record.action} {record.run_id or 'global'}",
            record,
            "subagent_dispatch_logged",
        )

    def _index_dispatch_watch_record(self, record: DispatchWatchRecord) -> None:
        self._index_dataclass_record(
            "subagent_dispatch_watch",
            record.id,
            f"Dispatch watch cycle {record.cycle}",
            record,
            "subagent_dispatch_watch_logged",
        )

    def _index_parent_planner_record(self, record: ParentPlannerRecord) -> None:
        self._index_dataclass_record(
            "parent_planner",
            record.id,
            f"Parent planner {record.decision}",
            record,
            "parent_planner_logged",
        )

    def _index_execution_context(self, context: SubAgentExecutionContext) -> None:
        self._index_dataclass_record(
            "subagent_execution_context",
            context.run_id,
            f"Execution context {context.run_id}",
            context,
            "subagent_execution_context_written",
        )

    def _index_runner_result(self, result: SubAgentRunnerResult, output_payload: dict[str, object]) -> None:
        content = "\n".join(
            [
                json.dumps(asdict(result), ensure_ascii=False, indent=2),
                "",
                "## Output",
                json.dumps(output_payload, ensure_ascii=False, indent=2),
            ]
        )
        self._log_local_record(
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

    def _index_channel_probe(self, result: ChannelProbeResult) -> None:
        self._index_dataclass_record(
            "subagent_channel_probe",
            f"{result.run_id}:{result.created_at:.6f}",
            f"Channel probe {result.run_id} {result.channel_status}",
            result,
            "subagent_channel_probe_logged",
        )

    def _index_dataclass_record(
        self,
        source_type: str,
        source_id: str,
        title: str,
        record: object,
        event_type: str,
    ) -> None:
        payload = asdict(record)
        metadata: dict[str, object] = {
            key: value
            for key, value in payload.items()
            if key
            in {
                "id",
                "run_id",
                "decision",
                "status",
                "ok",
                "applied",
                "dry_run",
                "created_at",
                "cycle",
                "backend",
                "tool_rounds",
            }
        }
        self._log_local_record(
            source_type=source_type,
            source_id=source_id,
            title=title,
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            metadata=metadata,
            event_type=event_type,
        )

    def _select_runs(self, run_ids: list[str] | None) -> list[SubAgentTask]:
        """按 run id 选择运行记录，过滤掉不可调度的状态。"""

        from .models import DISPATCH_INELIGIBLE_STATUSES

        if run_ids is None:
            all_runs = self.list_runs()
            # 过滤掉 DISPATCH_INELIGIBLE 状态
            return [r for r in all_runs if r.status not in DISPATCH_INELIGIBLE_STATUSES]
        if not run_ids:
            return []
        runs: list[SubAgentTask] = []
        for run_id in run_ids:
            try:
                task = self.load(run_id)
                if task.status not in DISPATCH_INELIGIBLE_STATUSES:
                    runs.append(task)
            except FileNotFoundError:
                continue
        return runs

    # Public aliases for internal indexing methods
    def select_runs(self, run_ids: list[str] | None) -> list[SubAgentTask]:
        """公开的选择运行记录方法。"""
        return self._select_runs(run_ids)

    def index_task(self, task: SubAgentTask) -> None:
        """公开的任务索引方法。"""
        return self._index_task(task)

    def index_dispatch_record(self, record: DispatchRecord) -> None:
        """公开的调度记录索引方法。"""
        return self._index_dispatch_record(record)

    def index_dispatch_watch_record(self, record: DispatchWatchRecord) -> None:
        """公开的调度观察记录索引方法。"""
        return self._index_dispatch_watch_record(record)

    def index_parent_planner_record(self, record: ParentPlannerRecord) -> None:
        """公开的父计划者记录索引方法。"""
        return self._index_parent_planner_record(record)

    def index_execution_context(self, context: SubAgentExecutionContext) -> None:
        """公开的执行上下文索引方法。"""
        return self._index_execution_context(context)

    def index_report(self, source_type: str, source_id: str, title: str, report: object, *, event_type: str) -> None:
        """公开的报告索引方法。"""
        return self._index_report(source_type, source_id, title, report, event_type=event_type)

    def log_local_record(self, *, source_type: str, source_id: str, title: str, content: str, metadata: dict[str, object] | None = None, event_type: str) -> None:
        """公开的本地记录方法。"""
        return self._log_local_record(source_type=source_type, source_id=source_id, title=title, content=content, metadata=metadata, event_type=event_type)
