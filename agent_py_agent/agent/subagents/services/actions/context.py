
from __future__ import annotations

"""shared bundles and record helpers for subagent action handlers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionHandlerContext:
    """Common audit fields for one action application."""

    before_status: str
    before_channel_status: str
    now: float
    take_over_by: str = ""
    locked_files: list[str] | None = None


@dataclass(frozen=True)
class CoordinatorHandoffErrorRequest:
    service: object
    action: object
    task: object
    ctx: ActionHandlerContext
    message: str


def coordinator_handoff_error_record(request: CoordinatorHandoffErrorRequest):
    from ...reports import ActionApplyRecord

    service = request.service
    action = request.action
    task = request.task
    ctx = request.ctx
    return ActionApplyRecord(
        id=service.manager._new_id("apply"),
        action_id=action.id,
        run_id=action.run_id,
        action=action.action,
        dry_run=False,
        applied=False,
        ok=False,
        message=request.message,
        before_status=ctx.before_status,
        after_status=ctx.before_status,
        before_channel_status=ctx.before_channel_status,
        after_channel_status=ctx.before_channel_status,
        evidence_paths=[task.task_dir],
        created_at=ctx.now,
    )
