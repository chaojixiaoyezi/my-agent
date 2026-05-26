# LLM: Tool-loop single-call execution owns runtime scope, trace, guard, and cleanup around one tool call.
# 模块用途: 从 ToolLoopService 拆出单个工具调用执行路径，保持服务类只负责循环编排。

from __future__ import annotations

from .runner_stage_trace import RunnerToolStageTraceRequest, trace_runner_tool_call_started
from .tool_api_collection_contract import normalize_api_collection_payload
from .tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
    guarded_tool_call_result,
)
from .tool_loop_recovery import payload_with_runtime_scope
from .tool_round_execution import ToolCallExecuteParams


# LLM: execute_one_tool_call performs one scoped guarded tool invocation.
# 函数用途: 设置当前工具循环参数、归一 payload、写 trace、执行 runtime guard，再恢复 agent 临时状态。
def execute_one_tool_call(agent, request: ToolCallExecuteParams):
    sentinel = object()
    previous = getattr(agent, "_current_tool_loop_params", sentinel)
    agent._current_tool_loop_params = request.params
    try:
        return _execute_scoped_tool_call(agent, request)
    finally:
        _restore_tool_loop_params(agent, previous, sentinel)


def _execute_scoped_tool_call(agent, request: ToolCallExecuteParams):
    payload = payload_with_runtime_scope(agent, request.params, request.payload)
    if isinstance(payload, dict):
        payload = normalize_api_collection_payload(request.params, payload)
    trace_request = RunnerToolStageTraceRequest(
        agent=agent,
        params=request.params,
        tool_rounds=request.tool_rounds,
        idx=request.idx,
        payload=payload,
    )
    trace_runner_tool_call_started(trace_request)
    runtime_request = ToolCallRuntimeRequest(agent, request, payload, trace_request)
    guard_result = guarded_tool_call_result(runtime_request)
    if guard_result is not None:
        return guard_result
    return execute_traced_tool_call(runtime_request)


def _restore_tool_loop_params(agent, previous: object, sentinel: object) -> None:
    if previous is sentinel:
        try:
            delattr(agent, "_current_tool_loop_params")
        except AttributeError:
            pass
        return
    agent._current_tool_loop_params = previous
