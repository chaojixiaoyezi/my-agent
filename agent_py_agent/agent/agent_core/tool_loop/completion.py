
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ...backends import ModelResponse
from .._runtime_params import ToolLoopExecuteParams
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


def _round_delivery_auto_closeout_ready(request: ToolRoundCompletionRequest) -> bool:
    if not isinstance(getattr(request.params, "delivery_contract", None), dict):
        return False
    if _round_submitted_for_acceptance(request):
        return False
    return any(str(item).startswith("[delivery-completion-soft-hint]") for item in getattr(request.params, "tool_context", []) or [])


def _is_task_local_round(request: ToolRoundCompletionRequest) -> bool:
    return str(getattr(request.params, "context_scope", "") or "").strip().lower() == "task_local"
