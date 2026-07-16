
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ...backends import ModelResponse
from .._runtime_params import ToolLoopExecuteParams
from ..delivery_closeout.closeout import (
    MainAgentDeliveryCloseoutRequest,
    main_agent_delivery_closeout_response,
)
from ..delivery_closeout.task_progress_gate import task_progress_has_open_items
from ..delivery_closeout.user_summary import (
    acceptance_user_summary,
    model_response_user_summary,
)
from ..delivery_completion_soft_hint import target_coverage_blocks_delivery_auto_closeout
from ..subagent.progress_closeout import subagent_progress_closeout_response
from .background_liveness import is_wake_capable_source
from .foreground_cooperative_yield import maybe_queue_foreground_cooperative_yield
from .natural_user_reply import queue_delivery_completion_user_reply, queue_natural_user_reply
from .round_execution import subagent_output_json_response


@dataclass(frozen=True)
class ToolRoundCompletionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    response: ModelResponse
    before_executed_count: int
    subagent_output_written: bool
    before_archive_count: int = 0
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
    if _round_submitted_for_acceptance(request) or _round_delivery_auto_closeout_ready(request):
        current_summary = model_response_user_summary(request.response)
        if _round_submitted_for_acceptance(request):
            current_summary = acceptance_user_summary(
                request.params.archive_tool_calls[request.before_archive_count :]
            ) or current_summary
        if delivery_response := main_agent_delivery_closeout_response(
            MainAgentDeliveryCloseoutRequest(
                agent=request.agent,
                params=request.params,
                backend=request.response.backend,
                user_summary=current_summary,
            )
        ):
            if queue_delivery_completion_user_reply(request.params, delivery_response):
                return None
            return delivery_response
    if maybe_queue_foreground_cooperative_yield(
        request.agent,
        request.params,
        tool_rounds=request.tool_rounds,
    ):
        return None
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


def _round_delivery_auto_closeout_ready(request: ToolRoundCompletionRequest) -> bool:
    if _round_submitted_for_acceptance(request):
        return False
    if not any(str(item).startswith("[delivery-completion-soft-hint]") for item in getattr(request.params, "tool_context", []) or []):
        return False
    # 模型自己列的 task_progress 待办还有没做完的 → 别在写完一个文件的【当轮】就主动收口。
    #   当轮主动收口会绕过 final_exit 的 todo 续航(_todo_persistence_decision,对齐
    #   终端应用 的 todo 驱动:模型自己列的清单没做完就踹回继续),把模型从"接着建 /
    #   派子代理"里当场切走(实锤:建站任务写完 SPEC.md 即 DELIVERY_COMPLETE、0 子代理、
    #   文档顶交付)。这里与出口续航同源守住:开放清单项存在就不主动放行,让模型做完自己
    #   的计划。没建过清单 / 清单已清空的普通交付(用户要个报告、产物落输出目录)不受影响,
    #   照常自动收口——判据只认模型自我声明的待办,避开 R9"逼凑数"老坑。
    if task_progress_has_open_items(request):
        return False
    # 终端应用 对齐(只限【无合同】run):模型这轮还在【动手】(写/改/跑/派/记账)就不当轮抢
    #   收口——auto-closeout 的本职是防"产物已在、只剩无限重读"的空转,不是打断建造。真机实锤
    #   (A1-u1 整合轮):无合同建站,第一轮写完 README 当轮即被收口切走,后续建造再没机会发生。
    #   无合同时收口没有客观完成判据(uncontracted 开放世界,一个 .md 就点灯),必须给模型继续
    #   的机会:只在"提示已注入、本轮零动手(纯读看)"的空转轮自动收口;模型主动
    #   submit_for_acceptance 或自然停(→final_exit 出口合同)不受影响。有合同的 run 保持当轮
    #   收口——合同 required 目标全部存在才点灯,收口就绪是客观事实,不存在"切断建造"问题。
    if not _has_delivery_contract(request.params) and _round_has_active_work(request):
        return False
    return not target_coverage_blocks_delivery_auto_closeout(request.agent, request.params)


_ROUND_ACTIVE_WORK_TOOLS = frozenset(
    {"write_file", "apply_patch", "edit_file", "run_command", "controlled_exec", "create_subagents", "task_progress"}
)


def _round_has_active_work(request: ToolRoundCompletionRequest) -> bool:
    executed = list(getattr(request.params, "executed_tools", []) or [])
    return any(name in _ROUND_ACTIVE_WORK_TOOLS for name in executed[request.before_executed_count :])


def _has_delivery_contract(params: object) -> bool:
    contract = getattr(params, "delivery_contract", None)
    return isinstance(contract, dict) and bool(contract)


def _is_task_local_round(request: ToolRoundCompletionRequest) -> bool:
    return str(getattr(request.params, "context_scope", "") or "").strip().lower() == "task_local"
