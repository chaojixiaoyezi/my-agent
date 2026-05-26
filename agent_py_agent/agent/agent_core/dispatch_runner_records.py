# LLM: Runner dispatch record builders keep batch execution focused on control flow.
# 模块用途: 构建 runner dry-run 和共享指令保护记录，不触发实际执行。

from __future__ import annotations

from dataclasses import dataclass

from ..subagents.services.dispatch_params import DispatchRecordParams
from .dispatch_params import DispatchContext


@dataclass(frozen=True)
class RunnerDryRecordParams:
    agent: object
    ctx: DispatchContext
    task: object
    before: object
    retry_reason: str


def dry_runner_record(params: RunnerDryRecordParams):
    return params.agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
            step="runner",
            action="retry_runner" if params.retry_reason else "execute_runner",
            run_id=params.task.id,
            dry_run=True,
            applied=False,
            ok=True,
            message=dry_runner_message(params.retry_reason),
            before_status=params.before.status,
            after_status=params.before.status,
            before_verification_status=params.before.verification_status,
            after_verification_status=params.before.verification_status,
            evidence_paths=[params.before.task_dir],
        ),
    )


def dry_runner_message(retry_reason: str) -> str:
    if retry_reason:
        return f"dry-run: 将重试 runner（{retry_reason}）。"
    return "dry-run: apply 时会生成执行上下文；带 --execute-runners 时会调用模型。"


def multi_runner_instruction_record(agent, ctx: DispatchContext, evidence_paths: list[str]):
    return agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
            step="runner_instruction",
            action="ignore_multi_runner_instruction",
            dry_run=not ctx.apply,
            applied=False,
            ok=True,
            message=(
                "已忽略本轮共享 runner_instruction：同一次 dispatch 选中了多个 runner，"
                "为避免某个子任务专属提示污染其他分支，请改为分别 dispatch 单个 run_id，"
                "或把通用要求写入每个 child goal/context bundle。"
            ),
            evidence_paths=evidence_paths[:20],
        ),
    )
