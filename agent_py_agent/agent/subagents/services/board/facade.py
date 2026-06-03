from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from ...models import SubAgentBoardOptions, SubAgentDueCheckOptions, SubAgentPlanActionsOptions
from ...policies import _filter_action_plan_items
from ...rendering import (
    render_action_plan_markdown,
    render_board_markdown,
    render_due_check_markdown,
)
from .items import build_risk_flags, to_board_item
from .service import SubAgentBoardService


class SubAgentBoardFacade:
    def __init__(self, manager: Any | None = None):
        if manager is not None:
            self._board_manager_ref = manager

    def _to_board_item(self, task):
        return self.to_board_item(task)

    def to_board_item(self, task):
        return to_board_item(self._manager, task)

    def _risk_flags(self, task, open_request_count=0, open_gap_count=0):
        return self.risk_flags(task, open_request_count, open_gap_count)

    def risk_flags(self, task, open_request_count=0, open_gap_count=0):
        return build_risk_flags(task, open_request_count, open_gap_count)

    def build_board(
        self,
        *,
        options: SubAgentBoardOptions | None = None,
        recent_limit: int = 20,
    ):
        board_options = _board_options(options, recent_limit=recent_limit)
        return self._service.build_board(options=board_options)

    def write_board(
        self,
        *,
        options: SubAgentBoardOptions | None = None,
        recent_limit: int = 20,
    ):
        board_options = _board_options(options, recent_limit=recent_limit)
        board = self.build_board(options=board_options)
        manager = self._manager
        (manager.workspace / "subagent_board.json").write_text(
            json.dumps(asdict(board), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (manager.workspace / "SUBAGENT_BOARD.md").write_text(
            render_board_markdown(board),
            encoding="utf-8",
        )
        return board

    def due_check(self, config=None, *, params: SubAgentDueCheckOptions | None = None):
        options = _due_check_options(config=config, params=params)
        return self._service.due_check(
            options.config,
            root_id=options.root_id,
            include_run_ids=options.include_run_ids,
            exclude_run_ids=options.exclude_run_ids,
        )

    def write_due_check(self, config=None, *, params: SubAgentDueCheckOptions | None = None):
        options = _due_check_options(config=config, params=params)
        report = self.due_check(params=options)
        manager = self._manager
        (manager.workspace / "subagent_due_check.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (manager.workspace / "SUBAGENT_DUE_CHECK.md").write_text(
            render_due_check_markdown(report),
            encoding="utf-8",
        )
        return report

    def plan_actions(self, config=None, *, params: SubAgentPlanActionsOptions | None = None):
        options = _plan_actions_options(config=config, params=params)
        return self._service.plan_actions(
            options.config,
            root_id=options.root_id,
            include_run_ids=options.include_run_ids,
            exclude_run_ids=options.exclude_run_ids,
        )

    def write_action_plan(self, config=None, *, params: SubAgentPlanActionsOptions | None = None):
        options = _plan_actions_options(config=config, params=params)
        report = self.plan_actions(params=options)
        manager = self._manager
        (manager.workspace / "subagent_action_plan.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (manager.workspace / "SUBAGENT_ACTION_PLAN.md").write_text(
            render_action_plan_markdown(report),
            encoding="utf-8",
        )
        return report

    def filter_action_plan_items(self, actions, action_filter="", run_id="", limit=0):
        return _filter_action_plan_items(actions, action_filter=action_filter, run_id=run_id, limit=limit)

    def _filter_action_plan_items(self, actions, action_filter="", run_id="", limit=0):
        return self.filter_action_plan_items(
            actions,
            action_filter=action_filter,
            run_id=run_id,
            limit=limit,
        )

    @property
    def _manager(self):
        return getattr(self, "_board_manager_ref", self)

    @property
    def _service(self) -> SubAgentBoardService:
        service = getattr(self, "_board_service_ref", None)
        if service is None:
            service = SubAgentBoardService(self._manager)
            self._board_service_ref = service
        return service


def _due_check_options(
    *,
    config,
    params: SubAgentDueCheckOptions | None,
) -> SubAgentDueCheckOptions:
    if params is not None:
        if not isinstance(params, SubAgentDueCheckOptions):
            raise TypeError("due check requires params: SubAgentDueCheckOptions")
        return params
    return SubAgentDueCheckOptions(config=config)


def _plan_actions_options(
    *,
    config,
    params: SubAgentPlanActionsOptions | None,
) -> SubAgentPlanActionsOptions:
    if params is not None:
        if not isinstance(params, SubAgentPlanActionsOptions):
            raise TypeError("plan actions requires params: SubAgentPlanActionsOptions")
        return params
    return SubAgentPlanActionsOptions(config=config)


def _board_options(
    options: SubAgentBoardOptions | None,
    *,
    recent_limit: int,
) -> SubAgentBoardOptions:
    if options is not None:
        if not isinstance(options, SubAgentBoardOptions):
            raise TypeError("board requires options: SubAgentBoardOptions")
        return options
    return SubAgentBoardOptions(recent_limit=recent_limit)


__all__ = ["SubAgentBoardFacade"]
