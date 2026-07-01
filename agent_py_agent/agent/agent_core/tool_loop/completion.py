
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ...backends import ModelResponse
from .._runtime_params import ToolLoopExecuteParams
from ..delivery_closeout.closeout import (
    MainAgentDeliveryCloseoutRequest,
    main_agent_delivery_closeout_response,
)
from ..delivery_completion_soft_hint import target_coverage_blocks_delivery_auto_closeout
from ..subagent.progress_closeout import subagent_progress_closeout_response
from .background_liveness import is_wake_capable_source
from .round_execution import subagent_output_json_response


@dataclass(frozen=True)
class ToolRoundCompletionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    response: ModelResponse
    before_executed_count: int
    subagent_output_written: bool


def completion_response_after_tool_round(
    request: ToolRoundCompletionRequest,
) -> ModelResponse | None:
    if _soft_wait_can_finish_turn(request) and _round_can_finish_via_soft_wait(request):
        return _soft_wait_response(request)
    if request.subagent_output_written:
        return subagent_output_json_response(request.agent, request.response, request.params)
    if _is_task_local_round(request):
        if progress_response := subagent_progress_closeout_response(request.agent, request.response):
            return progress_response
    if _round_submitted_for_acceptance(request) or _round_delivery_auto_closeout_ready(request):
        if delivery_response := main_agent_delivery_closeout_response(
            MainAgentDeliveryCloseoutRequest(
                agent=request.agent,
                params=request.params,
                backend=request.response.backend,
            )
        ):
            return delivery_response
    return None


def _round_submitted_for_acceptance(request: ToolRoundCompletionRequest) -> bool:
    executed = list(getattr(request.params, "executed_tools", []) or [])
    current_round = executed[request.before_executed_count :]
    return "submit_for_acceptance" in current_round


def _round_requested_soft_wait(request: ToolRoundCompletionRequest) -> bool:
    executed = list(getattr(request.params, "executed_tools", []) or [])
    current_round = executed[request.before_executed_count :]
    return "wait" in current_round


# LLM: Step4 放宽——本轮派了子代理(create_subagents)即可干净撒手,不必逼模型显式
#   调 wait。前提由调用方 gate 到 wake-capable 来源;此处再排除"本轮已提交验收 / 已写
#   子代理产物"两种要走收口的形态(它们优先走 delivery/subagent 收口,不能被 yield 抢短路)。
# 函数用途: 判断这轮能否以 soft-wait 干净结束(显式 wait,或派了子代理的非阻塞撒手)。
def _round_can_finish_via_soft_wait(request: ToolRoundCompletionRequest) -> bool:
    if _round_requested_soft_wait(request):
        return True
    if _round_submitted_for_acceptance(request) or request.subagent_output_written:
        return False
    return _round_dispatched_subagents(request)


def _round_dispatched_subagents(request: ToolRoundCompletionRequest) -> bool:
    executed = list(getattr(request.params, "executed_tools", []) or [])
    return "create_subagents" in executed[request.before_executed_count :]


def _soft_wait_can_finish_turn(request: ToolRoundCompletionRequest) -> bool:
    return is_wake_capable_source(request.params)


def _soft_wait_response(request: ToolRoundCompletionRequest) -> ModelResponse:
    return ModelResponse(
        text=(
            "已登记非阻塞等待提醒。子代理继续在后台运行；"
            "我这轮先不继续轮询，等提醒、完成事件或你的下一句话再继续处理。"
        ),
        backend=request.response.backend,
    )


def _round_delivery_auto_closeout_ready(request: ToolRoundCompletionRequest) -> bool:
    if _round_submitted_for_acceptance(request):
        return False
    if not any(str(item).startswith("[delivery-completion-soft-hint]") for item in getattr(request.params, "tool_context", []) or []):
        return False
    return not target_coverage_blocks_delivery_auto_closeout(request.agent, request.params)


def _is_task_local_round(request: ToolRoundCompletionRequest) -> bool:
    return str(getattr(request.params, "context_scope", "") or "").strip().lower() == "task_local"
