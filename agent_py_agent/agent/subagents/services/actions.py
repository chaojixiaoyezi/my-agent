from __future__ import annotations

"""LLM: action apply service for subagent tasks.

给人看的解释：
这里承接动作执行逻辑（apply_actions, _apply_action_item 等）。
SubAgentManager 通过 facade 方法委托到这里。
"""

import time
from typing import TYPE_CHECKING, Any

from .action_options import ActionApplyOptions
from .action_params import RecordAfterTaskActionParams
from .action_records import (
    ACTION_DISPATCH,
    ActionRecordContext,
    action_apply_summary,
    action_handler_context,
    append_action_apply_log,
    append_task_work_log,
    dry_run_action_record,
    missing_task_action_record,
    unsupported_action_record,
)
from .rescue_policy import action_rescue_record_fields

if TYPE_CHECKING:
    from ..models import ActionApplyRecord, ActionPlanItem
    from ..reports import ActionApplyRecord


class SubAgentActionService:
    """Action apply execution service."""

    def __init__(self, manager: Any):
        self.manager = manager

    def apply_actions(
        self,
        config: Any = None,
        options: ActionApplyOptions | None = None,
        *,
        apply: bool | None = None,
        action_filter: str | None = None,
        run_id: str | None = None,
        take_over_by: str | None = None,
        locked_files: list[str] | None = None,
        limit: int | None = None,
    ) -> ActionApplyReport:
        """Execute or dry-run an action plan."""
        from ..reports import ActionApplyReport

        opts = ActionApplyOptions.from_values(
            options,
            apply=apply,
            action_filter=action_filter,
            run_id=run_id,
            take_over_by=take_over_by,
            locked_files=locked_files,
            limit=limit,
        )
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
            summary=action_apply_summary(records),
            records=records,
        )

    def _apply_action_item(
        self,
        action: ActionPlanItem,
        *,
        options: ActionApplyOptions | None = None,
        apply: bool | None = None,
        action_filter: str | None = None,
        run_id: str | None = None,
        take_over_by: str | None = None,
        locked_files: list[str] | None = None,
        limit: int | None = None,
    ) -> ActionApplyRecord:
        """Execute a single action plan item."""
        opts = ActionApplyOptions.from_values(
            options,
            apply=apply,
            action_filter=action_filter,
            run_id=run_id,
            take_over_by=take_over_by,
            locked_files=locked_files,
            limit=limit,
        )
        now = time.time()
        try:
            task = self.manager.load(action.run_id)
        except FileNotFoundError as exc:
            return missing_task_action_record(ActionRecordContext(self.manager, action, now, opts=opts, exc=exc))

        before_status = task.status
        before_channel_status = task.channel_status
        if not opts.apply:
            return dry_run_action_record(
                ActionRecordContext(self.manager, action, now, before_status, before_channel_status, task)
            )

        handler = self._action_dispatch().get(action.action)
        if handler:
            return handler(
                self,
                action,
                task,
                action_handler_context(opts, now, before_status, before_channel_status),
            )

        return unsupported_action_record(
            ActionRecordContext(self.manager, action, now, before_status, before_channel_status, task)
        )

    def _action_dispatch(self) -> dict[str, callable]:
        return ACTION_DISPATCH

    def _record_after_task_action(
        self,
        params: RecordAfterTaskActionParams,
    ) -> ActionApplyRecord:
        """Create an apply record after task modification."""
        from ..reports import ActionApplyRecord

        return ActionApplyRecord(
            id=self.manager._new_id("apply"),
            action_id=params.action.id,
            run_id=params.action.run_id,
            action=params.action.action,
            dry_run=False,
            applied=True,
            ok=True,
            message=params.message,
            before_status=params.before_status,
            after_status=params.task.status,
            before_channel_status=params.before_channel_status,
            after_channel_status=params.task.channel_status,
            # LLM: apply logs preserve the rescue/escalation decision that led here.
            **action_rescue_record_fields(params.action),
            evidence_paths=params.evidence_paths or [params.task.work_log_file],
            created_at=time.time(),
        )

    def _append_action_apply_log(self, record: ActionApplyRecord) -> None:
        """Write global action apply audit log."""
        append_action_apply_log(self.manager, record)

    def _append_task_work_log(self, task: SubAgentTask, message: str) -> None:
        """Write apply progress to task's own WORK_LOG."""
        append_task_work_log(self.manager, task, message)
