# LLM: Body-read guard stage keeps ToolLoopService thin while preserving runner trace contracts.
# 模块用途: 在工具真正执行前应用委托期 refs-only 读正文策略，并补齐工具完成 trace。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..tools import ToolExecutionResult
from .orchestration_body_read_guard import (
    DelegatingBodyReadGuardRequest,
    maybe_block_delegating_body_read,
)
from .runner_stage_trace import RunnerToolStageTraceRequest, trace_runner_tool_call_finished
from .tool_round_execution import ToolCallExecuteParams


# LLM: ToolBodyReadGuardStageRequest bundles tool-loop stage data for parent refs-only policy.
# 类用途: 保存当前 agent、工具执行请求和作用域 payload，避免 ToolLoopService 直接知道守卫细节。
@dataclass(frozen=True)
class ToolBodyReadGuardStageRequest:
    __test__: ClassVar[bool] = False

    agent: object
    execute_request: ToolCallExecuteParams
    payload: object


# LLM: maybe_block_delegating_body_read_stage is the tool-loop integration point for body-read policy.
# 函数用途: 委托期读正文被阻断时返回工具结果并记录完成 trace；未触发返回 None。
def maybe_block_delegating_body_read_stage(
    request: ToolBodyReadGuardStageRequest,
) -> ToolExecutionResult | None:
    execute_request = request.execute_request
    result = maybe_block_delegating_body_read(
        DelegatingBodyReadGuardRequest(
            agent=request.agent,
            payload=request.payload,
            user_prompt=execute_request.params.user_prompt,
            task_attributes=execute_request.params.task_attributes,
        )
    )
    if result is None:
        return None
    trace_runner_tool_call_finished(
        RunnerToolStageTraceRequest(
            agent=request.agent,
            params=execute_request.params,
            tool_rounds=execute_request.tool_rounds,
            idx=execute_request.idx,
            payload=request.payload,
            result=result,
        )
    )
    return result
