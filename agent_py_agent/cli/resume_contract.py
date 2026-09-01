"""CLI 自动续跑契约层（2026-08-14 根因3 设计 v2）。

首轮 run 创建后，续跑轮显式贯穿同一 task/run/thread 链路——绝不靠
resume_context=True 或同一 conversation thread 自然形成（每轮 agent.run
默认会生成新 request_id/attempt_id/run_id/task_id，见 run_params.py 的
run_params_with_request_id）。本模块定义：
- CliContinuationContext：首轮创建的稳定身份，贯穿所有续跑轮
- 每轮 request_id 派生规则（{root}#cont-{seq}，ConversationStore append
  幂等键唯一且可追溯根）
- 续跑提示构造（只引用结构化 continuation_reason，不含验收语义）
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..agent.agent_core.runtime.loop_models import RunParams


@dataclass(frozen=True)
class CliContinuationContext:
    """CLI 自动续跑轮间贯穿的稳定身份（首轮创建，续跑轮复用）。"""

    root_task_id: str
    root_run_id: str
    root_thread_id: str
    # 首轮 request_id: conversation thread 的 channel_conversation_id 身份
    # (bind_cli_run_conversation 用 request_id 建 thread; 续跑轮复用同一
    # channel id 才能进同一 thread——root_run_id 可能是另一值, 不能当
    # channel 身份用)。
    root_request_id: str = ""
    continuation_seq: int = 0
    parent_attempt_id: str = ""

    def next(self, *, attempt_id: str) -> CliContinuationContext:
        """推进到下一轮：seq+1，parent_attempt 指向刚结束的轮。"""
        return replace(
            self,
            continuation_seq=self.continuation_seq + 1,
            parent_attempt_id=attempt_id,
        )

    def request_id_for(self, seq: int) -> str:
        """每轮 request_id 派生：{root}#cont-{seq}（幂等键唯一且可溯根）。"""
        return f"{self.root_run_id}#cont-{seq}"

    def apply_to(self, params: RunParams, *, seq: int, attempt_id: str) -> RunParams:
        """把契约身份写入 RunParams（续跑轮显式 ID 覆写，不新建根）。"""
        return replace(
            params,
            request_id=self.request_id_for(seq),
            run_id=self.root_run_id,
            task_id=self.root_task_id,
            attempt_id=attempt_id,
            continuation_seq=seq,
            continuation_root_task_id=self.root_task_id,
            continuation_root_run_id=self.root_run_id,
            continuation_root_thread_id=self.root_thread_id,
            continuation_root_request_id=self.root_request_id,
            continuation_parent_attempt_id=self.parent_attempt_id if seq > 0 else "",
        )


def continuation_from_params(params: RunParams) -> CliContinuationContext | None:
    """从 RunParams 还原契约（首轮 run 收口后读取，供 loop 续跑）。"""
    if not str(getattr(params, "continuation_root_task_id", "") or "").strip():
        return None
    return CliContinuationContext(
        root_task_id=str(params.continuation_root_task_id),
        root_run_id=str(params.continuation_root_run_id or params.run_id or ""),
        root_thread_id=str(params.continuation_root_thread_id or ""),
        root_request_id=str(
            getattr(params, "continuation_root_request_id", "") or params.request_id or ""
        ),
        continuation_seq=int(getattr(params, "continuation_seq", 0) or 0),
        parent_attempt_id=str(params.continuation_parent_attempt_id or ""),
    )


def record_budget_exhausted(
    *,
    agent: object,
    run_id: str,
    attempt_id: str,
    task_run_id: str = "",
    budget_before: int,
    budget_after: int,
    reason: str,
) -> None:
    """预算耗尽写 runtime_events continuation_budget_exhausted（fail-silent）。

    2026-08-14 设计 v2 审查意见5: max_rounds/resume_limit 只是预算, 不是
    成功条件——耗尽时必须写明确事件(含 budget before/after + 原因), 保留
    首轮与每个 attempt, 绝不输出 DONE。
    """
    try:
        subagents = getattr(agent, "subagents", None)
        repo = getattr(subagents, "runtime_db", None)
        if repo is None or not callable(getattr(repo, "append_event", None)):
            return
        if not run_id or not attempt_id:
            return
        row = repo.agent_run_for_run_id(run_id)
        if row is None:
            return
        repo.append_event(
            event_type="continuation_budget_exhausted",
            attempt_id=attempt_id,
            agent_run_id=str(row["agent_run_id"]),
            task_run_id=task_run_id,
            payload={
                "budget_before": int(budget_before or 0),
                "budget_after": int(budget_after or 0),
                "reason": str(reason or ""),
                "needs_user_continue": True,
                "stage": "cli_resume_loop",
            },
        )
    except Exception:  # noqa: BLE001 审计落账失败绝不反噬执行路径
        pass


def resume_prompt_for(
    *, continuation_reason: str, continuation_seq: int, user_task: str
) -> str:
    """续跑提示（唯一权威在 conversation/runtime.py, 此处转发兼容）。"""
    from ..agent.conversation.runtime import resume_prompt_for as _impl

    return _impl(
        continuation_reason=continuation_reason,
        continuation_seq=continuation_seq,
        user_task=user_task,
    )
