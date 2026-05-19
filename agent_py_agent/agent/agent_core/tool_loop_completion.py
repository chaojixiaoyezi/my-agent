# LLM: Tool loop completion helpers decide deterministic closeouts after a tool round.
# 模块用途: 工具轮结束后处理子代理 output.json 和顶层 dispatch 完成收口，避免完成后继续自由模型轮跑偏。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..backends import ModelResponse
from ._runtime_params import ToolLoopExecuteParams
from .subagent_dispatch_closeout import (
    DispatchCompletionRequest,
    subagent_dispatch_completion_response,
    subagent_dispatch_repair_required_response,
)
from .subagent_progress_closeout import subagent_progress_closeout_response
from .tool_round_execution import subagent_output_json_response


# LLM: ToolRoundCompletionRequest bundles post-tool-round closeout decisions.
# 类用途: 保存一轮工具执行后的收口判断字段，避免 ToolLoopService.execute 继续膨胀。
@dataclass(frozen=True)
class ToolRoundCompletionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    response: ModelResponse
    before_executed_count: int
    subagent_output_written: bool


# LLM: completion_response_after_tool_round short-circuits terminal runner and parent dispatch states.
# 函数用途: 子代理 runner 写出 output.json 后直接收口；顶层 dispatch 全绿后直接给结构化 refs，避免额外模型轮跑偏。
def completion_response_after_tool_round(
    request: ToolRoundCompletionRequest,
) -> ModelResponse | None:
    if request.subagent_output_written:
        return subagent_output_json_response(request.agent, request.response)
    if background_response := _background_intake_completion_response(request):
        return background_response
    if progress_response := subagent_progress_closeout_response(request.agent, request.response):
        return progress_response
    dispatch_request = DispatchCompletionRequest(
        agent=request.agent,
        params=request.params,
        before_executed_count=request.before_executed_count,
        backend=request.response.backend,
    )
    return (
        subagent_dispatch_completion_response(dispatch_request)
        or subagent_dispatch_repair_required_response(dispatch_request)
    )


# LLM: _background_intake_completion_response is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _background_intake_completion_response(request: ToolRoundCompletionRequest) -> ModelResponse | None:
    if not request.params.background_intake:
        return None
    tools = list(request.params.executed_tools[request.before_executed_count :])
    intake_tools = [tool for tool in tools if tool == "dispatch_subagents"]
    if not intake_tools:
        return None
    return ModelResponse(
        text=(
            "[background_intake_complete]\n"
            f"accepted_tools: {', '.join(intake_tools)}\n"
            "status: task cards accepted; runner execution is deferred to gateway daemon/worker pool.\n"
            "[/background_intake_complete]"
        ),
        backend=request.response.backend,
    )
