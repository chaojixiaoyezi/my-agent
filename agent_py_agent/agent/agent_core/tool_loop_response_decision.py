# LLM: Tool-loop response decisions stay separate from ToolLoopService control flow.
# 模块用途: 判断一次模型回复应该继续生成、结束，还是执行真实工具，并处理伪造工具回执。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._runtime_params import ToolLoopExecuteParams
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
    reserved_record_repairs: int


# LLM: ToolLoopResponseDecision stores the next action after parsing one model response.
# 类用途: 返回工具循环下一步动作、可执行工具调用、清理后的 response 和纠偏计数。
@dataclass(frozen=True)
class ToolLoopResponseDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[dict[str, object]]
    reserved_record_repairs: int


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
        return ToolLoopResponseDecision("break", final, [], request.reserved_record_repairs)

    calls = request.agent.tools.parse_tool_calls(request.response.text)
    if calls:
        clean_response = sanitize_reserved_tool_record_response(request.response)
        return ToolLoopResponseDecision("run_tools", clean_response, calls, request.reserved_record_repairs)

    return _no_tool_calls_decision(
        request.response,
        request.reserved_record_repairs,
        has_reserved_record,
    )


# LLM: _disabled_tools_response blocks fake transcript markers even when tools are disabled.
# 函数用途: 工具关闭时普通回复直接返回；如果模型仍伪造工具回执，则返回确定性阻断。
def _disabled_tools_response(response, has_reserved_record: bool):
    if not has_reserved_record:
        return response
    return reserved_tool_record_block_response(response.backend)


# LLM: _no_tool_calls_decision prevents spoof-only reserved records from becoming final answers.
# 函数用途: 无真实工具调用时，普通回复直接收口；伪造工具回执先纠偏一次，再重复就阻断。
def _no_tool_calls_decision(
    response,
    reserved_record_repairs: int,
    has_reserved_record: bool,
) -> ToolLoopResponseDecision:
    if not has_reserved_record:
        return ToolLoopResponseDecision("break", response, [], reserved_record_repairs)
    if reserved_record_repairs < 1:
        return ToolLoopResponseDecision("continue", None, [], reserved_record_repairs + 1)
    final = reserved_tool_record_block_response(response.backend)
    return ToolLoopResponseDecision("break", final, [], reserved_record_repairs)
