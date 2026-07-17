from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ...backends import ModelResponse
from .._runtime_params import ToolLoopExecuteParams
from ..runtime.active_turn_input import active_turn_user_input_texts
from ..runtime.task_identity import durable_task_id
from ..subagent.progress_closeout import subagent_progress_closeout_response
from .background_liveness import is_wake_capable_source
from .natural_user_reply import queue_natural_user_reply
from .round_execution import subagent_output_json_response

_MAX_WAIT_REPLY_REQUEST_CHARS = 4000
_MAX_WAIT_REPLY_GUIDANCE_CHARS = 1200
_MAX_WAIT_REPLY_GUIDANCE_ITEMS = 5


@dataclass(frozen=True)
class ToolRoundCompletionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    response: ModelResponse
    before_executed_count: int
    subagent_output_written: bool
    tool_rounds: int = 0


def completion_response_after_tool_round(
    request: ToolRoundCompletionRequest,
) -> ModelResponse | None:
    if _soft_wait_can_finish_turn(request) and _round_can_finish_via_soft_wait(request):
        _queue_soft_wait_user_reply(request)
        return None
    if request.subagent_output_written:
        return subagent_output_json_response(request.agent, request.response, request.params)
    if _is_task_local_round(request):
        if progress_response := subagent_progress_closeout_response(
            request.agent, request.response
        ):
            return progress_response
    return None


def _round_requested_soft_wait(request: ToolRoundCompletionRequest) -> bool:
    executed = list(getattr(request.params, "executed_tools", []) or [])
    current_round = executed[request.before_executed_count :]
    return "wait" in current_round


# LLM: 只有模型显式调用 wait 才能让出当前 turn；派出子代理本身不得
#   自动结束主 turn。这与 会话运行时 的单 thread/单 active turn 语义一致。
# 函数用途: 判断这轮是否由显式 wait 进入耐久等待。
def _round_can_finish_via_soft_wait(request: ToolRoundCompletionRequest) -> bool:
    return _round_requested_soft_wait(request)


def _soft_wait_can_finish_turn(request: ToolRoundCompletionRequest) -> bool:
    return is_wake_capable_source(request.params)


def _queue_soft_wait_user_reply(request: ToolRoundCompletionRequest) -> None:
    """Keep runtime facts authoritative while the model owns user-facing wording."""
    queue_natural_user_reply(
        request.params,
        kind="wait",
        facts=_soft_wait_reply_facts(request),
    )


def _soft_wait_reply_facts(request: ToolRoundCompletionRequest) -> dict[str, object]:
    params = request.params
    current_request = str(params.root_user_prompt or params.user_prompt or "").strip()
    facts: dict[str, object] = {
        "wait_registered": True,
        "reply_is_interim": True,
        "task_continues_without_more_user_input": True,
        "current_user_request": _bounded_reply_fact_text(
            current_request,
            _MAX_WAIT_REPLY_REQUEST_CHARS,
        ),
        "completed_action_count": len(list(params.executed_tools or [])),
        "tool_round_count": max(0, int(request.tool_rounds or 0)),
    }
    if len(current_request) > _MAX_WAIT_REPLY_REQUEST_CHARS:
        facts["current_user_request_truncated"] = True
    guidance = active_turn_user_input_texts(params.active_turn_user_inputs)
    if guidance:
        selected_guidance = guidance[-_MAX_WAIT_REPLY_GUIDANCE_ITEMS:]
        facts["current_user_guidance_count"] = len(guidance)
        facts["current_user_guidance"] = [
            _bounded_reply_fact_text(item, _MAX_WAIT_REPLY_GUIDANCE_CHARS)
            for item in selected_guidance
        ]
        if any(len(item) > _MAX_WAIT_REPLY_GUIDANCE_CHARS for item in selected_guidance):
            facts["current_user_guidance_truncated"] = True
    delegated = _delegated_work_facts(request.agent, params)
    if delegated:
        facts["delegated_work"] = delegated
    return facts


def _delegated_work_facts(agent: object, params: ToolLoopExecuteParams) -> dict[str, int]:
    task_id = durable_task_id(params)
    if not task_id:
        return {}
    try:
        from ...subagents.models import (
            SUBAGENT_ENDED_STATUSES,
            SUBAGENT_FAILURE_STATUSES,
            task_status_in,
        )

        run_ids = {
            str(item) for item in agent.subagent_run_ids_for_request(task_id) if str(item).strip()
        }
        runs = [
            run
            for run in agent.subagents.list_runs()
            if str(getattr(run, "id", "") or "") in run_ids
        ]
    except Exception:
        return {}
    if not runs:
        return {}
    active = sum(
        not task_status_in(getattr(run, "status", ""), SUBAGENT_ENDED_STATUSES) for run in runs
    )
    failed = sum(
        task_status_in(getattr(run, "status", ""), SUBAGENT_FAILURE_STATUSES) for run in runs
    )
    return {"total": len(runs), "active": active, "finished": len(runs) - active, "issues": failed}


def _bounded_reply_fact_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    marker = "\n…\n"
    if limit <= len(marker):
        return text[:limit]
    head = (limit - len(marker)) // 2
    tail = limit - len(marker) - head
    return text[:head] + marker + text[-tail:]


def _is_task_local_round(request: ToolRoundCompletionRequest) -> bool:
    return str(getattr(request.params, "context_scope", "") or "").strip().lower() == "task_local"
