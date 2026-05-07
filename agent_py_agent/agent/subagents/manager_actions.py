from __future__ import annotations

"""LLM contract: SubAgentActionMixin - thin facade delegating action service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/actions.py。
"""

from .models import SubAgentTask
from .reports import ActionApplyRecord, ActionPlanItem
from .services.action_options import ActionApplyOptions
from .services.action_params import RecordAfterTaskActionParams
from .services.actions import SubAgentActionService
from .services.indexing_params import IndexReportParams


class SubAgentActionMixin:
    """Thin facade delegating action apply execution to SubAgentActionService."""

    @property
    def _action_service(self):
        if not hasattr(self, "__action_service"):
            self.__action_service = SubAgentActionService(self)
        return self.__action_service

    # Internal methods for backward compatibility with tests
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
    ):
        return self._action_service._apply_action_item(
            action,
            options=options,
            apply=apply,
            action_filter=action_filter,
            run_id=run_id,
            take_over_by=take_over_by,
            locked_files=locked_files,
            limit=limit,
        )

    def _record_after_task_action(
        self,
        params: RecordAfterTaskActionParams,
    ):
        return self._action_service._record_after_task_action(params)

    def _append_action_apply_log(self, record: ActionApplyRecord):
        return self._action_service._append_action_apply_log(record)

    def _append_task_work_log(self, task: SubAgentTask, message: str):
        return self._action_service._append_task_work_log(task, message)

    def apply_actions(
        self,
        config=None,
        options: ActionApplyOptions | None = None,
        *,
        apply: bool | None = None,
        action_filter: str | None = None,
        run_id: str | None = None,
        take_over_by: str | None = None,
        locked_files: list[str] | None = None,
        limit: int | None = None,
    ):
        return self._action_service.apply_actions(
            config,
            ActionApplyOptions.from_values(
                options,
                apply=apply,
                action_filter=action_filter,
                run_id=run_id,
                take_over_by=take_over_by,
                locked_files=locked_files,
                limit=limit,
            ),
        )

    def write_action_apply_report(
        self,
        config=None,
        options: ActionApplyOptions | None = None,
        *,
        apply: bool | None = None,
        action_filter: str | None = None,
        run_id: str | None = None,
        take_over_by: str | None = None,
        locked_files: list[str] | None = None,
        limit: int | None = None,
    ):
        import json
        from dataclasses import asdict
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
        (self.workspace / "subagent_action_apply_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        from .rendering import render_action_apply_markdown
        (self.workspace / "SUBAGENT_ACTION_APPLY.md").write_text(
            render_action_apply_markdown(report), encoding="utf-8",
        )
        self._index_report(
            IndexReportParams(
                "subagent_action_apply_report", "latest",
                "Subagent action apply report", report,
                "subagent_action_apply_report_written",
            ),
        )
        return report
