from __future__ import annotations

"""LLM contract: SubAgentActionMixin - thin facade delegating action service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/actions.py。
"""

from .services.action_options import ActionApplyOptions
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

    def apply_actions(self, config=None, options: ActionApplyOptions | None = None, **overrides):
        return self._action_service.apply_actions(
            config,
            ActionApplyOptions.from_values(options, **overrides),
        )

    def write_action_apply_report(self, config=None, options: ActionApplyOptions | None = None, **overrides):
        import json
        from dataclasses import asdict
        opts = ActionApplyOptions.from_values(options, **overrides)
        report = self.apply_actions(config, opts)
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
