
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ...backends import ModelResponse
from .._runtime_params import ToolLoopExecuteParams
from ..subagent.progress_closeout import subagent_progress_closeout_response
from .background_liveness import is_wake_capable_source
from .foreground_cooperative_yield import maybe_queue_foreground_cooperative_yield
from .natural_user_reply import queue_natural_user_reply
from .round_execution import subagent_output_json_response


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
        if progress_response := subagent_progress_closeout_response(request.agent, request.response):
            return progress_response
    if maybe_queue_foreground_cooperative_yield(
        request.agent,
        request.params,
        tool_rounds=request.tool_rounds,
    ):
        return None
    return None


def _round_requested_soft_wait(request: ToolRoundCompletionRequest) -> bool:
    executed = list(getattr(request.params, "executed_tools", []) or [])
    current_round = executed[request.before_executed_count :]
    return "wait" in current_round


# LLM: Step4 放宽——本轮派了子代理(create_subagents)即可干净撒手,不必逼模型显式
#   调 wait。前提由调用方 gate 到 wake-capable 来源；子代理 runner 已写结构化结果时
#   仍由 runner 自己的结果出口处理，不能被普通后台回执抢短路。
# 函数用途: 判断这轮能否以 soft-wait 干净结束(显式 wait,或派了子代理的非阻塞撒手)。
def _round_can_finish_via_soft_wait(request: ToolRoundCompletionRequest) -> bool:
    if _round_requested_soft_wait(request):
        return True
    if request.subagent_output_written:
        return False
    return _round_dispatched_subagents(request)


def _round_dispatched_subagents(request: ToolRoundCompletionRequest) -> bool:
    executed = list(getattr(request.params, "executed_tools", []) or [])
    return "create_subagents" in executed[request.before_executed_count :]


def _soft_wait_can_finish_turn(request: ToolRoundCompletionRequest) -> bool:
    return is_wake_capable_source(request.params)


def _queue_soft_wait_user_reply(request: ToolRoundCompletionRequest) -> None:
    """Keep runtime facts authoritative while the model owns user-facing wording."""
    if not _round_dispatched_subagents(request):
        queue_natural_user_reply(
            request.params,
            kind="wait",
            facts={"wait_registered": True, "reply_is_interim": True},
        )
        return
    lifecycle = _schedule_lifecycle(request.params.archive_tool_calls)
    counts = lifecycle.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    recorded = _lifecycle_count(counts.get("recorded"), lifecycle.get("requested_count"))
    accepted = _lifecycle_count(counts.get("accepted"), lifecycle.get("accepted_run_ids"))
    running = _lifecycle_count(counts.get("running"), lifecycle.get("running_run_ids"))
    failed = _lifecycle_count(counts.get("failed"), lifecycle.get("failed_run_ids"))
    queue_natural_user_reply(
        request.params,
        kind="background_dispatch",
        facts={
            "work_items_planned": recorded,
            "work_items_ready": accepted,
            "work_items_started": running,
            "work_items_failed_to_start": failed,
            "reply_is_interim": True,
            "user_can_continue_conversation": True,
        },
    )


def _schedule_lifecycle(records: object) -> dict[str, object]:
    records = list(records or [])
    lifecycles: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, dict) or str(record.get("tool") or "") != "create_subagents":
            continue
        envelope = record.get("tool_result_envelope")
        if not isinstance(envelope, dict):
            continue
        lifecycle = envelope.get("schedule_lifecycle")
        if isinstance(lifecycle, dict):
            lifecycles.append(dict(lifecycle))
    if not lifecycles:
        return {}
    if len(lifecycles) == 1:
        return lifecycles[0]
    return _aggregate_schedule_lifecycles(lifecycles)


def _aggregate_schedule_lifecycles(lifecycles: list[dict[str, object]]) -> dict[str, object]:
    """Combine independent same-round create calls into one truthful receipt."""
    recorded_ids = _merged_lifecycle_ids(lifecycles, "recorded_run_ids")
    accepted_ids = _merged_lifecycle_ids(lifecycles, "accepted_run_ids")
    running_ids = _merged_lifecycle_ids(lifecycles, "running_run_ids")
    failed_ids = _merged_lifecycle_ids(lifecycles, "failed_run_ids")
    requested_count = sum(_lifecycle_count(item.get("requested_count"), []) for item in lifecycles)
    return {
        "requested_count": requested_count,
        "recorded_run_ids": recorded_ids,
        "accepted_run_ids": accepted_ids,
        "running_run_ids": running_ids,
        "failed_run_ids": failed_ids,
        "counts": {
            "recorded": len(recorded_ids) or _summed_lifecycle_count(lifecycles, "recorded"),
            "accepted": len(accepted_ids) or _summed_lifecycle_count(lifecycles, "accepted"),
            "running": len(running_ids) or _summed_lifecycle_count(lifecycles, "running"),
            "failed": len(failed_ids) or _summed_lifecycle_count(lifecycles, "failed"),
        },
    }


def _merged_lifecycle_ids(lifecycles: list[dict[str, object]], key: str) -> list[str]:
    merged: list[str] = []
    for lifecycle in lifecycles:
        values = lifecycle.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            text = str(value or "").strip()
            if text and text not in merged:
                merged.append(text)
    return merged


def _summed_lifecycle_count(lifecycles: list[dict[str, object]], key: str) -> int:
    total = 0
    for lifecycle in lifecycles:
        counts = lifecycle.get("counts")
        counts = counts if isinstance(counts, dict) else {}
        total += _lifecycle_count(counts.get(key), [])
    return total


def _lifecycle_count(primary: object, fallback: object) -> int:
    try:
        return max(0, int(primary))
    except (TypeError, ValueError):
        return len(fallback) if isinstance(fallback, list) else 0


def _is_task_local_round(request: ToolRoundCompletionRequest) -> bool:
    return str(getattr(request.params, "context_scope", "") or "").strip().lower() == "task_local"
