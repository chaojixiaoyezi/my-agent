from __future__ import annotations

"""LLM: action apply service for subagent tasks.

给人看的解释：
这里承接动作执行逻辑（apply_actions, _apply_action_item 等）。
SubAgentManager 通过 facade 方法委托到这里。
"""

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .action_handlers import (
    ActionHandlerContext,
    apply_probe_or_repair_channel,
    apply_record_only_action,
    apply_reopen_for_evidence,
    apply_repair_work_order,
    apply_run_acceptance,
    apply_takeover_or_reassign,
)
from .action_options import ActionApplyOptions
from .rescue_policy import action_rescue_record_fields

if TYPE_CHECKING:
    from ..models import ActionApplyRecord, ActionPlanItem, SubAgentTask
    from ..reports import ActionApplyRecord


class SubAgentActionService:
    """Action apply execution service."""

    def __init__(self, manager: Any):
        self.manager = manager

    def apply_actions(
        self,
        config: Any = None,
        options: ActionApplyOptions | None = None,
        **overrides,
    ) -> ActionApplyReport:
        """Execute or dry-run an action plan."""
        from ..reports import ActionApplyReport

        opts = ActionApplyOptions.from_values(options, **overrides)
        plan = self.manager.plan_actions(config)
        actions = self.manager._filter_action_plan_items(
            plan.actions,
            action_filter=opts.action_filter,
            run_id=opts.run_id,
            limit=opts.limit,
        )
        records: list[ActionApplyRecord] = []
        for action in actions:
            record = self._apply_action_item(action, options=opts)
            records.append(record)
            if opts.apply:
                self._append_action_apply_log(record)

        return ActionApplyReport(
            generated_at=time.time(),
            dry_run=not opts.apply,
            summary=_action_apply_summary(records),
            records=records,
        )

    def _apply_action_item(
        self,
        action: ActionPlanItem,
        *,
        options: ActionApplyOptions | None = None,
        **overrides,
    ) -> ActionApplyRecord:
        """Execute a single action plan item."""
        from ..reports import ActionApplyRecord

        opts = ActionApplyOptions.from_values(options, **overrides)
        now = time.time()
        try:
            task = self.manager.load(action.run_id)
        except FileNotFoundError as exc:
            return _missing_task_action_record(self.manager, action, opts, now, exc)

        before_status = task.status
        before_channel_status = task.channel_status
        if not opts.apply:
            return _dry_run_action_record(
                self.manager,
                action,
                task,
                now,
                before_status=before_status,
                before_channel_status=before_channel_status,
            )

        handler = self._action_dispatch().get(action.action)
        if handler:
            ctx = ActionHandlerContext(
                before_status=before_status,
                before_channel_status=before_channel_status,
                now=now,
                take_over_by=opts.take_over_by,
                locked_files=opts.locked_files or [],
            )
            return handler(self, action, task, ctx)

        return ActionApplyRecord(
            id=self.manager._new_id("apply"),
            action_id=action.id, run_id=action.run_id, action=action.action,
            dry_run=False, applied=False, ok=False,
            message=f"暂不支持 apply 动作: {action.action}",
            before_status=before_status, after_status=before_status,
            before_channel_status=before_channel_status, after_channel_status=before_channel_status,
            evidence_paths=[task.task_dir], created_at=now,
        )

    def _action_dispatch(self) -> dict[str, callable]:
        return {
            "probe_or_repair_channel": apply_probe_or_repair_channel,
            "inspect_channel_probe": apply_probe_or_repair_channel,
            "repair_work_order": apply_repair_work_order,
            "reopen_for_evidence": apply_reopen_for_evidence,
            "run_acceptance": apply_run_acceptance,
            "takeover_or_reassign": apply_takeover_or_reassign,
            "route_capability_request": apply_record_only_action,
            "triage_capability_gap": apply_record_only_action,
            "inspect_failure": apply_record_only_action,
            "classify_blocker": apply_record_only_action,
        }

    def _record_after_task_action(
        self,
        action: ActionPlanItem,
        task: SubAgentTask,
        before_status: str,
        before_channel_status: str,
        message: str,
        *,
        evidence_paths: list[str] | None = None,
    ) -> ActionApplyRecord:
        """Create an apply record after task modification."""
        from ..reports import ActionApplyRecord
        return ActionApplyRecord(
            id=self.manager._new_id("apply"),
            action_id=action.id,
            run_id=action.run_id,
            action=action.action,
            dry_run=False,
            applied=True,
            ok=True,
            message=message,
            before_status=before_status,
            after_status=task.status,
            before_channel_status=before_channel_status,
            after_channel_status=task.channel_status,
            # LLM: apply logs preserve the rescue/escalation decision that led here.
            **action_rescue_record_fields(action),
            evidence_paths=evidence_paths or [task.work_log_file],
            created_at=time.time(),
        )

    def _append_action_apply_log(self, record: ActionApplyRecord) -> None:
        """Write global action apply audit log."""
        from dataclasses import asdict

        from ...file_io import append_jsonl

        jsonl = self.manager.workspace / "subagent_action_apply_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.manager.workspace / "ACTION_APPLY_LOG.md"
        if not markdown.exists():
            markdown.write_text("# ACTION APPLY LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} run={record.run_id} action={record.action} "
                f"applied={record.applied} message={record.message}\n"
            )
        self.manager._index_action_apply(record)

    def _append_task_work_log(self, task: SubAgentTask, message: str) -> None:
        """Write apply progress to task's own WORK_LOG."""
        from dataclasses import asdict

        path = Path(task.work_log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("# WORK_LOG\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"- {time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        self.manager._log_local_record(
            source_type="subagent_work_log",
            source_id=f"{task.id}:{time.time():.6f}",
            title=f"Subagent work log {task.id}",
            content=f"{task.id}\n{task.goal}\n{message}",
            metadata={
                "run_id": task.id,
                "goal": task.goal,
                "status": task.status,
                "verification_status": task.verification_status,
                "work_log_file": task.work_log_file,
            },
            event_type="subagent_work_log_appended",
        )


def _action_apply_summary(records: list[ActionApplyRecord]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(records)}
    for record in records:
        summary[record.action] = summary.get(record.action, 0) + 1
        status_key = "ok" if record.ok else "failed"
        mode_key = "applied" if record.applied else "dry_run"
        summary[status_key] = summary.get(status_key, 0) + 1
        summary[mode_key] = summary.get(mode_key, 0) + 1
    return summary


def _missing_task_action_record(
    manager: Any,
    action: ActionPlanItem,
    opts: ActionApplyOptions,
    now: float,
    exc: FileNotFoundError,
) -> ActionApplyRecord:
    from ..reports import ActionApplyRecord

    return ActionApplyRecord(
        id=manager._new_id("apply"),
        action_id=action.id, run_id=action.run_id, action=action.action,
        dry_run=not opts.apply, applied=False, ok=False, message=str(exc),
        **action_rescue_record_fields(action),
        created_at=now,
    )


def _dry_run_action_record(
    manager: Any,
    action: ActionPlanItem,
    task: SubAgentTask,
    now: float,
    *,
    before_status: str,
    before_channel_status: str,
) -> ActionApplyRecord:
    from ..reports import ActionApplyRecord

    return ActionApplyRecord(
        id=manager._new_id("apply"),
        action_id=action.id, run_id=action.run_id, action=action.action,
        dry_run=True, applied=False, ok=True,
        message=f"dry-run: would {action.action}",
        before_status=before_status, after_status=before_status,
        before_channel_status=before_channel_status, after_channel_status=before_channel_status,
        **action_rescue_record_fields(action),
        evidence_paths=[task.task_dir], created_at=now,
    )
