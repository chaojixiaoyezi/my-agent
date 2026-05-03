from __future__ import annotations

"""LLM contract: SubAgentBoardMixin - thin facade delegating board service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
业务逻辑已移至 services/board.py。
"""

from .services.board import SubAgentBoardService, _to_board_item, _build_risk_flags


class SubAgentBoardMixin:
    """Thin facade delegating board, due-check, and action planning to SubAgentBoardService."""

    @property
    def _board_service(self):
        if not hasattr(self, "__board_service"):
            self.__board_service = SubAgentBoardService(self)
        return self.__board_service

    def _to_board_item(self, task):
        return _to_board_item(self, task)

    def _risk_flags(self, task, open_request_count=0, open_gap_count=0):
        return _build_risk_flags(task, open_request_count, open_gap_count)

    def build_board(self, *, recent_limit: int = 20):
        return self._board_service.build_board(recent_limit=recent_limit)

    def write_board(self, *, recent_limit: int = 20):
        board = self.build_board(recent_limit=recent_limit)
        import json
        from dataclasses import asdict
        (self.workspace / "subagent_board.json").write_text(
            json.dumps(asdict(board), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        from .rendering import render_board_markdown
        (self.workspace / "SUBAGENT_BOARD.md").write_text(
            render_board_markdown(board), encoding="utf-8",
        )
        return board

    def due_check(self, config=None):
        return self._board_service.due_check(config)

    def write_due_check(self, config=None):
        report = self.due_check(config)
        import json
        from dataclasses import asdict
        (self.workspace / "subagent_due_check.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        from .rendering import render_due_check_markdown
        (self.workspace / "SUBAGENT_DUE_CHECK.md").write_text(
            render_due_check_markdown(report), encoding="utf-8",
        )
        return report

    def plan_actions(self, config=None):
        return self._board_service.plan_actions(config)

    def write_action_plan(self, config=None):
        report = self.plan_actions(config)
        import json
        from dataclasses import asdict
        (self.workspace / "subagent_action_plan.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        from .rendering import render_action_plan_markdown
        (self.workspace / "SUBAGENT_ACTION_PLAN.md").write_text(
            render_action_plan_markdown(report), encoding="utf-8",
        )
        return report

    # Internal helpers used by board service
    def _filter_action_plan_items(self, actions, action_filter="", run_id="", limit=0):
        from .policies import _filter_action_plan_items as _filter_items
        return _filter_items(actions, action_filter=action_filter, run_id=run_id, limit=limit)