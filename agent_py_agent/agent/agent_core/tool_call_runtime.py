
from __future__ import annotations

import json
from dataclasses import dataclass

from ..tooling.models import ToolExecutionResult
from .audit_dispatch import audit_privileged_tool_call
from .parameters import _one_shot_tool_call_key
from .runner.stage_trace import RunnerToolStageTraceRequest, trace_runner_tool_call_finished
from .subagent.attempt_guard import stale_subagent_attempt_result
from .tool_guard.agent_budget_stage import (
    ToolAgentBudgetStageRequest,
    maybe_block_tool_agent_budget,
)
from .tool_loop.recovery import tool_payload_with_run_scope
from .tool_loop.round_execution import ToolCallExecuteParams
from .tool_runtime_ledger import write_boundary_with_runtime_ledger


@dataclass(frozen=True)
class ToolCallRuntimeRequest:
    agent: object
    request: ToolCallExecuteParams
    payload: dict[str, object]
    trace_request: RunnerToolStageTraceRequest


def guarded_tool_call_result(runtime_request: ToolCallRuntimeRequest):
    request = runtime_request.request
    payload = runtime_request.payload
    trace_request = runtime_request.trace_request
    one_shot_key = _one_shot_tool_call_key(payload)
    if one_shot_key and one_shot_key in request.params.one_shot_tool_calls:
        result = _duplicate_one_shot_result(payload)
        return _trace_finished_result(trace_request, result)
    stale_result = stale_subagent_attempt_result(runtime_request.agent, payload)
    if stale_result is not None:
        return _trace_finished_result(trace_request, stale_result)
    return maybe_block_tool_agent_budget(ToolAgentBudgetStageRequest(runtime_request.agent, request, payload))


def execute_traced_tool_call(runtime_request: ToolCallRuntimeRequest):
    one_shot_key = _one_shot_tool_call_key(runtime_request.payload)
    executable_payload = tool_payload_with_run_scope(
        runtime_request.agent,
        runtime_request.request.params,
        runtime_request.payload,
        call_id=_runtime_tool_call_id(runtime_request),
    )
    result = runtime_request.agent.tools.execute_call(
        executable_payload,
        allowed_tools=runtime_request.request.params.allowed_tools,
        granted_capabilities=runtime_request.request.params.granted_capabilities,
        write_boundary=write_boundary_with_runtime_ledger(runtime_request.agent, runtime_request.request.params),
    )
    audit_privileged_tool_call(runtime_request.agent, executable_payload, result)  # 特权动作落审计(审计 #13)
    if one_shot_key and _one_shot_result_consumes_key(result):
        runtime_request.request.params.one_shot_tool_calls.add(one_shot_key)
    trace_runner_tool_call_finished(_finished_trace_request(runtime_request, result))
    return result


def _runtime_tool_call_id(runtime_request: ToolCallRuntimeRequest) -> str:
    return f"round-{runtime_request.trace_request.tool_rounds}-tool-{runtime_request.trace_request.idx}"


def _duplicate_one_shot_result(payload: dict[str, object]) -> ToolExecutionResult:
    tool_name = str(payload.get("tool") or "unknown")
    return ToolExecutionResult(
        tool_name,
        False,
        "本轮已经执行过相同的一次性编排工具调用，系统已阻止重复执行。"
        "请基于前面的工具结果直接给最终回答，不要再次调用同一个工具。",
    )


def _trace_finished_result(
    trace_request: RunnerToolStageTraceRequest,
    result: ToolExecutionResult,
):
    trace_runner_tool_call_finished(_finished_trace_request_from_trace(trace_request, result))
    return result


def _finished_trace_request(runtime_request: ToolCallRuntimeRequest, result: ToolExecutionResult):
    return _finished_trace_request_from_trace(runtime_request.trace_request, result)


def _finished_trace_request_from_trace(trace_request: RunnerToolStageTraceRequest, result: ToolExecutionResult):
    return RunnerToolStageTraceRequest(
        agent=trace_request.agent,
        params=trace_request.params,
        tool_rounds=trace_request.tool_rounds,
        idx=trace_request.idx,
        payload=trace_request.payload,
        result=result,
    )


def _one_shot_result_consumes_key(result: ToolExecutionResult) -> bool:
    if not result.ok:
        return False
    return not _orchestration_result_is_blocked(result.output)


def _orchestration_result_is_blocked(output: object) -> bool:
    try:
        payload = json.loads(str(output or ""))
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("blocked"))
