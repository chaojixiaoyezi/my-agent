# LLM: Tool-loop response decisions stay separate from ToolLoopService control flow.
# 模块用途: 判断一次模型回复应该继续生成、结束，还是执行真实工具，并处理伪造工具回执。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._runtime_params import ToolLoopExecuteParams
from .tool_open_write_session_repair import (
    open_write_session_block_response,
    open_write_session_repair_context,
)
from .tool_reserved_record_guard import (
    contains_reserved_tool_record,
    reserved_tool_record_block_response,
    reserved_tool_record_repair_context,
    sanitize_reserved_tool_record_response,
)


# LLM: ToolLoopResponseDecisionRequest bundles response parsing inputs for code-size guardrails.
# 类用途: 集中保存工具循环决策所需上下文，避免 helper 函数参数继续扩散。
@dataclass(frozen=True)
class ToolLoopResponseDecisionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    response: object
    counters: ToolLoopRepairCounters


# LLM: ToolLoopRepairCounters keeps repair bookkeeping out of ToolLoopService positional params.
# 类用途: 保存工具循环里的纠偏计数，避免每新增一种修复都扩散方法签名。
@dataclass(frozen=True)
class ToolLoopRepairCounters:
    __test__: ClassVar[bool] = False

    reserved_record_repairs: int = 0
    open_write_session_repairs: int = 0


# LLM: ToolLoopResponseDecision stores the next action after parsing one model response.
# 类用途: 返回工具循环下一步动作、可执行工具调用、清理后的 response 和纠偏计数。
@dataclass(frozen=True)
class ToolLoopResponseDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[dict[str, object]]
    counters: ToolLoopRepairCounters


# LLM: _NoToolCallsRequest bundles final-answer checks so helper signatures stay stable.
# 类用途: 保存无工具调用时判断收口、纠偏或阻断需要的上下文字段。
@dataclass(frozen=True)
class _NoToolCallsRequest:
    agent: object
    params: ToolLoopExecuteParams
    response: object
    counters: ToolLoopRepairCounters
    has_reserved_record: bool


# LLM: tool_loop_response_decision centralizes fake-record repair and tool-call parsing.
# 函数用途: 根据模型回复决定继续生成、结束或执行真实工具；伪造系统回执只给一次纠偏机会。
def tool_loop_response_decision(
    request: ToolLoopResponseDecisionRequest,
) -> ToolLoopResponseDecision:
    has_reserved_record = contains_reserved_tool_record(request.response.text)
    if has_reserved_record:
        request.params.tool_context.append(reserved_tool_record_repair_context())

    if not request.agent.config.enable_tools:
        final = _disabled_tools_response(request.response, has_reserved_record)
        return ToolLoopResponseDecision("break", final, [], request.counters)

    calls = request.agent.tools.parse_tool_calls(request.response.text)
    if calls:
        clean_response = sanitize_reserved_tool_record_response(request.response)
        return ToolLoopResponseDecision("run_tools", clean_response, calls, request.counters)

    return _no_tool_calls_decision(
        _NoToolCallsRequest(
            request.agent,
            request.params,
            request.response,
            request.counters,
            has_reserved_record,
        )
    )


# LLM: _disabled_tools_response blocks fake transcript markers even when tools are disabled.
# 函数用途: 工具关闭时普通回复直接返回；如果模型仍伪造工具回执，则返回确定性阻断。
def _disabled_tools_response(response, has_reserved_record: bool):
    if not has_reserved_record:
        return response
    return reserved_tool_record_block_response(response.backend)


# LLM: _no_tool_calls_decision prevents spoof-only reserved records from becoming final answers.
# 函数用途: 无真实工具调用时，普通回复直接收口；伪造工具回执先纠偏一次，再重复就阻断。
def _no_tool_calls_decision(request: _NoToolCallsRequest) -> ToolLoopResponseDecision:
    open_session_decision = _open_write_session_decision(request)
    if open_session_decision is not None:
        return open_session_decision
    if not request.has_reserved_record:
        return ToolLoopResponseDecision("break", request.response, [], request.counters)
    if request.counters.reserved_record_repairs < 1:
        return ToolLoopResponseDecision("continue", None, [], _inc_reserved(request.counters))
    final = reserved_tool_record_block_response(request.response.backend)
    return ToolLoopResponseDecision("break", final, [], request.counters)


# LLM: _open_write_session_decision blocks final prose while chunked writes remain open.
# 函数用途: 基于 manifest 事实判断是否需要继续 finish/abort 分块写入会话。
def _open_write_session_decision(
    request: _NoToolCallsRequest,
) -> ToolLoopResponseDecision | None:
    open_session_context = open_write_session_repair_context(
        request.agent,
        request.counters.open_write_session_repairs,
    )
    if open_session_context:
        request.params.tool_context.append(open_session_context)
        return ToolLoopResponseDecision("continue", None, [], _inc_open_session(request.counters))
    if request.counters.open_write_session_repairs:
        block = open_write_session_block_response(request.agent)
        if block is not None:
            return ToolLoopResponseDecision("break", block, [], request.counters)
    return None


# LLM: _inc_reserved returns a new counters bundle after fake-record repair.
# 函数用途: 增加 reserved record 纠偏计数，保持 dataclass 不可变。
def _inc_reserved(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs + 1,
        open_write_session_repairs=counters.open_write_session_repairs,
    )


# LLM: _inc_open_session returns a new counters bundle after open-session repair.
# 函数用途: 增加分块写入 session 纠偏计数，保持 dataclass 不可变。
def _inc_open_session(counters: ToolLoopRepairCounters) -> ToolLoopRepairCounters:
    return ToolLoopRepairCounters(
        reserved_record_repairs=counters.reserved_record_repairs,
        open_write_session_repairs=counters.open_write_session_repairs + 1,
    )
