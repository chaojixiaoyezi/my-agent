# LLM: Tool loop completion helpers decide only runner-level deterministic closeouts after a tool round.
# 模块用途: 工具轮结束后处理子代理 output.json 收口；顶层 dispatch 完成后交回 root 继续综合最终交付。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..backends import ModelResponse
from ._runtime_params import ToolLoopExecuteParams
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


# LLM: completion_response_after_tool_round short-circuits runner output contracts but lets parent synthesis continue.
# 函数用途: 子代理 runner 写出 output.json 后直接收口；顶层 dispatch 完成后不本地替用户总结，而是回到 root 生成交付。
def completion_response_after_tool_round(
    request: ToolRoundCompletionRequest,
) -> ModelResponse | None:
    if request.subagent_output_written:
        return subagent_output_json_response(request.agent, request.response)
    return None
