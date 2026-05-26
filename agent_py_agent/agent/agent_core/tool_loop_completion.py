# LLM: Tool loop completion helpers decide deterministic closeouts after a tool round.
# 模块用途: 工具轮结束后处理子代理 output.json、显式交付提交和顶层 dispatch 收口；普通工具轮不做最终验收。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..backends import ModelResponse
from ._runtime_params import ToolLoopExecuteParams
from .main_agent_delivery_closeout import (
    MainAgentDeliveryCloseoutRequest,
    main_agent_delivery_closeout_response,
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


# LLM: completion_response_after_tool_round only accepts explicit terminal or repair signals after tools run.
# 函数用途: 子代理 runner 写出 output.json 后直接收口；主代理只有显式 submit_for_acceptance 或明确修复阻塞才做机器收口。
def completion_response_after_tool_round(
    request: ToolRoundCompletionRequest,
) -> ModelResponse | None:
    if request.subagent_output_written:
        return subagent_output_json_response(request.agent, request.response)
    if progress_response := subagent_progress_closeout_response(request.agent, request.response):
        return progress_response
    if _round_submitted_for_acceptance(request):
        if delivery_response := main_agent_delivery_closeout_response(
            MainAgentDeliveryCloseoutRequest(
                agent=request.agent,
                params=request.params,
                backend=request.response.backend,
            )
        ):
            return delivery_response
    return None


# LLM: submit_for_acceptance is the only normal tool-round trigger for main-agent delivery closeout.
# 函数用途: 从本轮新增的成功工具记录判断模型是否显式交卷，避免 read/fetch/write 中间态被提前验收。
def _round_submitted_for_acceptance(request: ToolRoundCompletionRequest) -> bool:
    executed = list(getattr(request.params, "executed_tools", []) or [])
    current_round = executed[request.before_executed_count :]
    return "submit_for_acceptance" in current_round
