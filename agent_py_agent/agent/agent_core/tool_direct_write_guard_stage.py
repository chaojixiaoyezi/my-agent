# LLM: Direct-write guard stage keeps ToolLoopService orchestration slim.
# 模块用途: 在工具执行前应用“委托任务不能由 root 直接写产物”的策略，并写入 trace。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..tools import ToolExecutionResult
from .orchestration_direct_write_guard import (
    DelegateOnlyDirectWriteGuardRequest,
    maybe_block_delegate_only_direct_write,
)
from .runner_stage_trace import RunnerToolStageTraceRequest, trace_runner_tool_call_finished
from .tool_round_execution import ToolCallExecuteParams


# LLM: ToolDirectWriteGuardStageRequest bundles one pending tool call for direct-write policy.
# 类用途: 保存 agent、工具执行请求和作用域 payload，方便测试和后续扩展豁免规则。
@dataclass(frozen=True)
class ToolDirectWriteGuardStageRequest:
    __test__: ClassVar[bool] = False

    agent: object
    execute_request: ToolCallExecuteParams
    payload: object


# LLM: maybe_block_delegate_only_direct_write_stage integrates the guard into the tool loop.
# 函数用途: 如果直接写产物被阻断，返回工具结果并补 trace；未触发时返回 None。
def maybe_block_delegate_only_direct_write_stage(
    request: ToolDirectWriteGuardStageRequest,
) -> ToolExecutionResult | None:
    execute_request = request.execute_request
    result = maybe_block_delegate_only_direct_write(
        DelegateOnlyDirectWriteGuardRequest(
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
