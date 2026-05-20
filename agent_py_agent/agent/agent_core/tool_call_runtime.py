# LLM: Tool-call runtime helpers isolate guard checks and finished traces from the main tool loop.
# 模块用途: 执行单个工具调用前的拦截、预算、去重和 runner trace，不让 tool loop 主流程变长。

from __future__ import annotations

import json
from dataclasses import dataclass

from ..tools import ToolExecutionResult
from .parameters import _one_shot_tool_call_key
from .runner_stage_trace import RunnerToolStageTraceRequest, trace_runner_tool_call_finished
from .subagent_attempt_guard import stale_subagent_attempt_result
from .tool_agent_budget_stage import ToolAgentBudgetStageRequest, maybe_block_tool_agent_budget
from .tool_body_read_guard_stage import (
    ToolBodyReadGuardStageRequest,
    maybe_block_delegating_body_read_stage,
)
from .tool_call_guardrail import maybe_block_repeated_tool_failure
from .tool_direct_write_guard_stage import (
    ToolDirectWriteGuardStageRequest,
    maybe_block_delegate_only_direct_write_stage,
)
from .tool_round_execution import ToolCallExecuteParams


# LLM: ToolCallRuntimeRequest bundles one parsed tool call with its trace metadata.
# 类用途: 把工具执行、guard 和 trace 需要的字段放进一个参数包，避免 helper 参数膨胀。
@dataclass(frozen=True)
class ToolCallRuntimeRequest:
    agent: object
    request: ToolCallExecuteParams
    payload: dict[str, object]
    trace_request: RunnerToolStageTraceRequest


# LLM: guarded_tool_call_result centralizes pre-execution blocks for one tool call.
# 函数用途: 在真正执行工具前统一处理一次性去重、过期子代理尝试、读正文限制、直接写入限制和预算限制。
def guarded_tool_call_result(runtime_request: ToolCallRuntimeRequest):
    request = runtime_request.request
    payload = runtime_request.payload
    trace_request = runtime_request.trace_request
    one_shot_key = _one_shot_tool_call_key(payload)
    if one_shot_key and one_shot_key in request.params.one_shot_tool_calls:
        result = _duplicate_one_shot_result(payload)
        return _trace_finished_result(trace_request, result)
    repeated_failure_result = maybe_block_repeated_tool_failure(
        runtime_request.agent,
        request.params,
        payload,
    )
    if repeated_failure_result is not None:
        return _trace_finished_result(trace_request, repeated_failure_result)
    stale_result = stale_subagent_attempt_result(runtime_request.agent, payload)
    if stale_result is not None:
        return _trace_finished_result(trace_request, stale_result)
    body_read_result = maybe_block_delegating_body_read_stage(
        ToolBodyReadGuardStageRequest(runtime_request.agent, request, payload)
    )
    if body_read_result is not None:
        return body_read_result
    direct_write_result = maybe_block_delegate_only_direct_write_stage(
        ToolDirectWriteGuardStageRequest(runtime_request.agent, request, payload)
    )
    if direct_write_result is not None:
        return direct_write_result
    return maybe_block_tool_agent_budget(ToolAgentBudgetStageRequest(runtime_request.agent, request, payload))


# LLM: execute_traced_tool_call runs the tool and writes the runner finished trace.
# 函数用途: 执行工具调用、登记一次性编排 key，并写入 finished trace。
def execute_traced_tool_call(runtime_request: ToolCallRuntimeRequest):
    one_shot_key = _one_shot_tool_call_key(runtime_request.payload)
    result = runtime_request.agent.tools.execute_call(
        runtime_request.payload,
        allowed_tools=runtime_request.request.params.allowed_tools,
        granted_capabilities=runtime_request.request.params.granted_capabilities,
        write_boundary=runtime_request.request.params.write_boundary,
    )
    if one_shot_key and _one_shot_result_consumes_key(result):
        runtime_request.request.params.one_shot_tool_calls.add(one_shot_key)
    trace_runner_tool_call_finished(_finished_trace_request(runtime_request, result))
    return result


# LLM: _duplicate_one_shot_result gives the model a deterministic stop signal for repeated orchestration calls.
# 函数用途: 同一轮重复调用 create/schedule 这类一次性工具时，返回可读阻断结果。
def _duplicate_one_shot_result(payload: dict[str, object]) -> ToolExecutionResult:
    tool_name = str(payload.get("tool") or "unknown")
    return ToolExecutionResult(
        tool_name,
        False,
        "本轮已经执行过相同的一次性编排工具调用，系统已阻止重复执行。"
        "请基于前面的工具结果直接给最终回答，不要再次调用同一个工具。",
    )


# LLM: _trace_finished_result keeps guard branches short while preserving runner trace symmetry.
# 函数用途: 工具调用被 guard 提前拦截时，统一写 finished trace 并返回同一个 ToolExecutionResult。
def _trace_finished_result(
    trace_request: RunnerToolStageTraceRequest,
    result: ToolExecutionResult,
):
    trace_runner_tool_call_finished(_finished_trace_request_from_trace(trace_request, result))
    return result


# LLM: _finished_trace_request projects runtime request facts into the trace dataclass.
# 函数用途: 为正常工具完成路径生成 runner_tool_call_finished 事件。
def _finished_trace_request(runtime_request: ToolCallRuntimeRequest, result: ToolExecutionResult):
    return _finished_trace_request_from_trace(runtime_request.trace_request, result)


# LLM: _finished_trace_request_from_trace avoids duplicating finished trace field mapping.
# 函数用途: 复用 started trace 的 agent/params/round/index/payload 字段，并附加 result。
def _finished_trace_request_from_trace(trace_request: RunnerToolStageTraceRequest, result: ToolExecutionResult):
    return RunnerToolStageTraceRequest(
        agent=trace_request.agent,
        params=trace_request.params,
        tool_rounds=trace_request.tool_rounds,
        idx=trace_request.idx,
        payload=trace_request.payload,
        result=result,
    )


# LLM: _one_shot_result_consumes_key preserves retry room for semantic orchestration blocks.
# 函数用途: 只有真正成功推进的 create/schedule 调用才登记去重；blocked=true 允许上层修正后重试。
def _one_shot_result_consumes_key(result: ToolExecutionResult) -> bool:
    if not result.ok:
        return False
    return not _orchestration_result_is_blocked(result.output)


# LLM: _orchestration_result_is_blocked detects JSON schedule payloads that did not mutate the tree.
# 函数用途: schedule_child_subagents 可能 ok=True 但返回 blocked=true；这类结果不应吃掉一次性调用名额。
def _orchestration_result_is_blocked(output: object) -> bool:
    try:
        payload = json.loads(str(output or ""))
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("blocked"))
