# LLM: Runner dispatch record rendering is separate from candidate and retry policy.
# 模块用途: 把 runner 执行前后状态写成 dispatch record，避免 runner_dispatch.py 继续膨胀。

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..subagent import SubAgentRunnerResult, SubAgentTask
from ..subagents.services.dispatch_params import DispatchRecordParams
from .runner_child_summary import runner_child_summary_fields

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: RunnerDispatchRecordParams carries before/after/result facts for one runner dispatch.
# 类用途: 集中保存调度记录参数字段，让记录渲染不依赖外层 batch 局部变量。
@dataclass(frozen=True)
class RunnerDispatchRecordParams:
    agent: SimpleAgent
    run_id: str
    before: SubAgentTask
    after: SubAgentTask
    result: SubAgentRunnerResult
    retry_reason: str
    execute_runners: bool


# LLM: runner_dispatch_record renders one machine-readable runner dispatch row.
# 函数用途: 记录执行前后状态、是否 dry-run、证据路径和 child summary 字段。
def runner_dispatch_record(params: RunnerDispatchRecordParams):
    return params.agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
            step="runner",
            action=_runner_action(params),
            run_id=params.run_id,
            dry_run=params.result.dry_run,
            applied=not params.result.dry_run,
            ok=params.result.ok,
            message=params.result.message,
            before_status=params.before.status,
            after_status=params.after.status,
            before_verification_status=params.before.verification_status,
            after_verification_status=params.after.verification_status,
            evidence_paths=[
                params.result.execution_context_json,
                params.result.result_json,
                params.result.output_json,
            ],
            **runner_child_summary_fields(params.agent, params.after, params.result),
        ),
    )


# LLM: _runner_action keeps record action naming consistent across retry/dry-run paths.
# 函数用途: 根据 retry_reason 和 execute_runners 返回 dispatch record action。
def _runner_action(params: RunnerDispatchRecordParams) -> str:
    if params.retry_reason and params.execute_runners:
        return "retry_runner"
    if params.execute_runners:
        return "execute_runner"
    return "runner_dry_run"


__all__ = ["RunnerDispatchRecordParams", "runner_dispatch_record"]
