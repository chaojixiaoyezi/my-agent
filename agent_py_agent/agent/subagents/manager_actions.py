from __future__ import annotations

"""LLM contract: SubAgentActionMixin - thin facade delegating action service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/actions.py。
"""

from .services.actions import SubAgentActionService


class SubAgentActionMixin:
    """Thin facade delegating action apply execution to SubAgentActionService."""

    @property
    def _action_service(self):
        if not hasattr(self, "__action_service"):
            self.__action_service = SubAgentActionService(self)
        return self.__action_service

    # Internal methods for backward compatibility with tests
    def _apply_action_item(self, *args, **kwargs):
        return self._action_service._apply_action_item(*args, **kwargs)

    def _record_after_task_action(self, *args, **kwargs):
        return self._action_service._record_after_task_action(*args, **kwargs)

    def _append_action_apply_log(self, *args, **kwargs):
        return self._action_service._append_action_apply_log(*args, **kwargs)

    def _append_task_work_log(self, *args, **kwargs):
        return self._action_service._append_task_work_log(*args, **kwargs)

    def apply_actions(self, config=None, *, apply=False, action_filter="", run_id="", take_over_by="", locked_files=None, limit=0):
        return self._action_service.apply_actions(
            config, apply=apply, action_filter=action_filter, run_id=run_id,
            take_over_by=take_over_by, locked_files=locked_files, limit=limit,
        )

    def write_action_apply_report(self, config=None, *, apply=False, action_filter="", run_id="", take_over_by="", locked_files=None, limit=0):
        import json
        from dataclasses import asdict
        report = self.apply_actions(
            config, apply=apply, action_filter=action_filter, run_id=run_id,
            take_over_by=take_over_by, locked_files=locked_files, limit=limit,
        )
        (self.workspace / "subagent_action_apply_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        from .rendering import render_action_apply_markdown
        (self.workspace / "SUBAGENT_ACTION_APPLY.md").write_text(
            render_action_apply_markdown(report), encoding="utf-8",
        )
        self._index_report(
            "subagent_action_apply_report", "latest",
            "Subagent action apply report", report,
            event_type="subagent_action_apply_report_written",
        )
        return report