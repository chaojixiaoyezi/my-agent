
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ...backends import ModelResponse
from .._runtime_params import ToolLoopExecuteParams
from ..delivery_completion_soft_hint import target_coverage_blocks_delivery_auto_closeout
from ..delivery_closeout.closeout import (
    MainAgentDeliveryCloseoutRequest,
    main_agent_delivery_closeout_response,
)
from ..subagent.progress_closeout import subagent_progress_closeout_response
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
    if _round_requested_soft_wait(request) and _soft_wait_can_finish_turn(request):
        return _soft_wait_response(request)
    if request.subagent_output_written:
        return subagent_output_json_response(request.agent, request.response)
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


def _soft_wait_can_finish_turn(request: ToolRoundCompletionRequest) -> bool:
    source = str(getattr(request.params, "source", "") or "").strip()
    return source in {"background_main_agent", "chat", "gateway"}


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
