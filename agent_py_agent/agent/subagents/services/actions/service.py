
from __future__ import annotations

"""action apply service for subagent tasks.

这里承接动作执行逻辑（apply_actions, _apply_action_item 等）。
SubAgentManager 通过 facade 方法委托到这里。
"""

import time
from typing import TYPE_CHECKING, Any

from ...models import SubAgentPlanActionsOptions
from ..indexing.params import IndexReportParams
from ..rescue_policy import action_rescue_record_fields
from .options import ActionApplyOptions
from .params import RecordAfterTaskActionParams
from .records import (
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

if TYPE_CHECKING:
    from ...reports import ActionApplyRecord, ActionPlanItem


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
        include_run_ids: list[str] | None = None,
    ) -> ActionApplyReport:
        """Execute or dry-run an action plan."""
        opts = ActionApplyOptions.from_values(
            options,
            apply=apply,
            action_filter=action_filter,
            run_id=run_id,
            take_over_by=take_over_by,
            locked_files=locked_files,
            limit=limit,
            include_run_ids=include_run_ids,
        )
        records = self._apply_action_plan_records(config, opts)
        return _action_apply_report_from_records(opts, records)

    def write_action_apply_report(
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
        import json
        from dataclasses import asdict

        from ...rendering import render_action_apply_markdown

        opts = ActionApplyOptions.from_values(
            options,
            apply=apply,
            action_filter=action_filter,
            run_id=run_id,
            take_over_by=take_over_by,
            locked_files=locked_files,
            limit=limit,
        )
        report = self.apply_actions(config, opts)
        (self.manager.workspace / "subagent_action_apply_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.manager.workspace / "SUBAGENT_ACTION_APPLY.md").write_text(
            render_action_apply_markdown(report),
            encoding="utf-8",
        )
        self.manager._index_report(
            IndexReportParams(
                "subagent_action_apply_report",
                "latest",
                "Subagent action apply report",
                report,
                "subagent_action_apply_report_written",
            ),
        )
        return report

    def _apply_action_plan_records(self, config: Any, opts: ActionApplyOptions) -> list[ActionApplyRecord]:
        plan = self.manager.plan_actions(
            config,
            params=_plan_options_from_action_options(config, opts),
        )
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
        return records

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
        from ...reports import ActionApplyRecord

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


def _plan_options_from_action_options(config: Any, opts: ActionApplyOptions) -> SubAgentPlanActionsOptions:
    return SubAgentPlanActionsOptions(
        config=config,
        root_id=opts.root_id,
        exclude_run_ids=list(opts.exclude_run_ids or []),
        include_run_ids=list(opts.include_run_ids or []),
    )


def _action_apply_report_from_records(
    opts: ActionApplyOptions,
    records: list[ActionApplyRecord],
) -> ActionApplyReport:
    from ...reports import ActionApplyReport

    return ActionApplyReport(
        generated_at=time.time(),
        dry_run=not opts.apply,
        summary=action_apply_summary(records),
        records=records,
    )
