
from __future__ import annotations

from ..runner.stage_trace import RunnerToolStageTraceRequest, trace_runner_tool_call_started
from ..tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
    guarded_tool_call_result,
)
from .recovery import payload_with_runtime_scope
from .round_execution import ToolCallExecuteParams


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
