# LLM: Shared action-apply context bundles live here so handlers stay small and composable.
# 模块用途: 提供 action handler 共享的上下文参数包和失败记录构造，避免各动作模块互相复制字段。

from __future__ import annotations

"""shared bundles and record helpers for subagent action handlers."""

from dataclasses import dataclass


# LLM: ActionHandlerContext belongs to the subagent action layer; keep audit fields explicit.
# 类用途: 集中保存动作 handler 的前置状态、时间戳和接管参数，避免 handler 函数散传业务参数。
@dataclass(frozen=True)
class ActionHandlerContext:
    """Common audit fields for one action application."""

    before_status: str
    before_channel_status: str
    now: float
    take_over_by: str = ""
    locked_files: list[str] | None = None


# LLM: CoordinatorHandoffErrorRequest bundles failed handoff record inputs to keep helper signatures stable.
# 类用途: 保存 coordinator 领导权恢复失败记录所需上下文，避免 helper 继续散传参数。
@dataclass(frozen=True)
class CoordinatorHandoffErrorRequest:
    service: object
    action: object
    task: object
    ctx: ActionHandlerContext
    message: str


# LLM: coordinator_handoff_error_record returns a stable failed apply record without mutating task state.
# 函数用途: 构造 coordinator 领导权恢复失败记录，保持缺 leader / leader 不存在时的审计格式一致。
def coordinator_handoff_error_record(request: CoordinatorHandoffErrorRequest):
    from ..reports import ActionApplyRecord

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
