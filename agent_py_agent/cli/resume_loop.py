"""CLI 自动续跑循环（2026-08-14 根因3 设计 v2，审查通过后实施）。

cmd_run 首轮收口后，若收口是可续跑族（共享 gate should_continue_task），
进程内循环续跑——同一 task/run/thread 链路（CliContinuationContext 贯穿），
直到：任务完成 / 预算耗尽 / 不可续跑族（blocked/协议违规/UNKNOWN）/ 用户
停止。续跑轮不走 cli_one_shot failed 兜底（切片3），任务级非终态由本循环
决定。

防失控双保险：resume_limit（policy 预算，默认 3，ensure_ordinary_task_resume
单一权威递增）+ max_rounds（进程内护栏，默认 8）。预算耗尽写
continuation_budget_exhausted 事件，绝不输出 DONE。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..agent.agent_core.cli_run_conversation import bind_cli_run_conversation
from ..agent.agent_core.runtime.loop_models import RunParams
from .resume_contract import (
    CliContinuationContext,
    record_budget_exhausted,
    resume_prompt_for,
)


@dataclass(frozen=True)
class CliResumeOutcome:
    """续跑循环结果（结构化，供 cmd_run 收口与审计）。"""

    status: str  # completed / budget_exhausted / stopped / unresumable
    rounds: int  # 首轮 + 续跑轮数
    final_result: object = None
    reason: str = ""


def should_resume(result: object) -> tuple[bool, str]:
    """收口后是否续跑（共享 gate，gateway 与 CLI 同一判断）。"""
    from ..agent.conversation.runtime import should_continue_task

    return should_continue_task(result)


class ResumeRunOnce:
    """首轮/续跑轮的 run 封装：契约 ID 贯穿同一 task/run/thread 链路。

    - 首轮：bind_cli_run_conversation 显式绑定（thread 建在
      ConversationStore），从绑定 params 构建 CliContinuationContext。
    - 续跑轮：ctx.apply_to 覆写 ID（request_id=root#cont-N, run/task=
      root, thread 复用），resume_context=True。
    """

    def __init__(
        self,
        agent: object,
        *,
        base_params: RunParams,
        on_chunk: object = None,
        save: bool = False,
        delivery_contract: object = None,
    ):
        self.agent = agent
        self.base_params = base_params
        self.on_chunk = on_chunk
        self.save = save
        self.delivery_contract = delivery_contract
        self.ctx: CliContinuationContext | None = None

    def __call__(self, prompt: str, continuation_seq: int = 0, attempt_id: str = "") -> tuple:
        """返回 (result, bound_params)。首轮后 self.ctx 建立。"""
        if continuation_seq == 0:
            bound = bind_cli_run_conversation(self.agent, self.base_params, prompt)
            result = self.agent.run(
                prompt,
                params=bound,
                save=self.save,
                source="cli_run",
                delivery_contract=self.delivery_contract,
                on_chunk=self.on_chunk,
            )
            thread_id = str(
                (bound.task_attributes or {}).get("conversation_thread_id") or ""
            )
            # parent_attempt 记首轮实际 attempt_id(bind 后 run_params 生成),
            # 续跑轮 apply_to 用它作树形链起点。
            self.ctx = CliContinuationContext(
                root_task_id=str(bound.task_id or bound.run_id or ""),
                root_run_id=str(bound.run_id or bound.request_id or ""),
                root_thread_id=thread_id,
                root_request_id=str(bound.request_id or ""),
                parent_attempt_id=str(bound.attempt_id or ""),
            )
            return result, bound
        if self.ctx is None:
            raise RuntimeError("resume round without first-round continuation context")
        bound = self.ctx.apply_to(
            self.base_params, seq=continuation_seq, attempt_id=attempt_id
        )
        result = self.agent.run(
            prompt,
            params=bound,
            save=self.save,
            source="cli_run",
            resume_context=True,
            delivery_contract=self.delivery_contract,
            on_chunk=self.on_chunk,
        )
        return result, bound


def run_with_resume(
    agent: object,
    *,
    initial_prompt: str,
    base_params: RunParams,
    max_rounds: int = 8,
    on_chunk: object = None,
    save: bool = False,
    delivery_contract: object = None,
) -> CliResumeOutcome:
    """首轮 + 续跑轮循环（进程内，同一 task/run/thread 链路）。"""
    runner = ResumeRunOnce(
        agent,
        base_params=base_params,
        on_chunk=on_chunk,
        save=save,
        delivery_contract=delivery_contract,
    )
    result, _bound = runner(initial_prompt, 0, "")
    rounds = 1
    should, reason = should_resume(result)
    if not should:
        return CliResumeOutcome(
            status="unresumable", rounds=rounds, final_result=result, reason=reason
        )
    if runner.ctx is None or not runner.ctx.root_task_id:
        return CliResumeOutcome(
            status="unresumable", rounds=rounds, final_result=result,
            reason="no_continuation_contract",
        )

    for seq in range(1, max_rounds + 1):
        prompt = resume_prompt_for(
            continuation_reason=reason,
            continuation_seq=seq,
            user_task=initial_prompt,
        )
        result, _bound = runner(prompt, seq, attempt_id=f"attempt-{seq}")
        rounds += 1
        # 每轮后推进契约 parent_attempt：下一轮的 parent = 本轮 attempt
        runner.ctx = runner.ctx.next(attempt_id=f"attempt-{seq}")
        should, reason = should_resume(result)
        if not should:
            return CliResumeOutcome(
                status="completed", rounds=rounds, final_result=result, reason=reason
            )
        if rounds >= max_rounds + 1:
            record_budget_exhausted(
                agent=agent,
                run_id=runner.ctx.root_run_id,
                attempt_id=str(getattr(result, "attempt_id", "") or ""),
                budget_before=rounds - 1,
                budget_after=rounds,
                reason="max_rounds_reached",
            )
            return CliResumeOutcome(
                status="budget_exhausted", rounds=rounds, final_result=result,
                reason="max_rounds_reached",
            )
    record_budget_exhausted(
        agent=agent,
        run_id=runner.ctx.root_run_id,
        attempt_id=str(getattr(result, "attempt_id", "") or ""),
        budget_before=rounds,
        budget_after=rounds,
        reason="resume_limit_reached",
    )
    return CliResumeOutcome(
        status="budget_exhausted", rounds=rounds, final_result=result,
        reason="resume_limit_reached",
    )
