from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ...backends import ModelResponse
from ...conversation.active_turn_input import active_turn_user_input_texts
from .._runtime_params import ToolLoopExecuteParams
from ..runtime.task_identity import durable_task_id
from ..subagent.progress_closeout import subagent_progress_closeout_response
from .background_liveness import is_wake_capable_source
from .natural_user_reply import queue_natural_user_reply
from .round_execution import subagent_output_json_response

_MAX_WAIT_REPLY_REQUEST_CHARS = 4000
_MAX_WAIT_REPLY_GUIDANCE_CHARS = 1200
_MAX_WAIT_REPLY_GUIDANCE_ITEMS = 5
_TASK_PROGRESS_NUDGE_STATE_KEY = "_task_progress_completion_nudge"
_TASK_PROGRESS_NUDGE_TAG = "[tool-system:task-progress-completion-check]"


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
    facts = _interim_reply_facts(
        request.agent,
        request.params,
        tool_rounds=request.tool_rounds,
    )
    facts["wait_registered"] = True
    return facts


# LLM: root 模型在仍有非终态 child 时输出普通文本，运行时把它当作 interim draft 丢弃；
#   只按 canonical child state 触发一次新的无工具表达轮，不解析草稿里的“完成/稍后”等自然语言。
# 函数用途: 阻止主代理在子代理尚未收齐时结束当前用户 turn，并登记一条模型自然撰写的阶段回复。
def queue_interim_reply_for_open_subagents(
    agent: object,
    params: ToolLoopExecuteParams,
    *,
    tool_rounds: int,
) -> bool:
    # 会话运行时 keeps parent and child turns as distinct sessions.  A child turn
    # returns its machine-readable result to the parent; it never enters the
    # parent's user-facing presentation phase.  ``context_scope`` is our
    # structured session boundary, so do not infer this from prompt wording or
    # agent names.
    if str(getattr(params, "context_scope", "") or "") == "task_local":
        return False
    delegated = _delegated_work_facts(agent, params)
    if not _delegated_work_has_open_runs(delegated):
        return False
    queue_natural_user_reply(
        params,
        kind="subagents_active",
        facts=_interim_reply_facts(
            agent,
            params,
            tool_rounds=tool_rounds,
            delegated=delegated,
        ),
    )
    return True


# LLM: open progress only triggers one structured reminder inside the existing tool-capable loop;
# it must never become an ordinary-task completion gate or a presentation-only model turn.
# 函数用途: 主代理准备结束但任务清单仍有开放项时，提醒它再核对一次，并保留原来的工具能力。
def queue_task_progress_completion_nudge(
    agent: object,
    params: ToolLoopExecuteParams,
    *,
    tool_rounds: int,
) -> bool:
    """Give the tool-capable model loop one soft verification nudge.

    This adapts 终端交互's structural verification reminder without routing
    through the presentation-only reply loop and without adding an ordinary
    task completion gate.  The next model turn can read/update ``task_progress``
    with its normal tools.  A per-turn typed marker makes the reminder one-shot;
    no model wording is parsed.
    """

    if str(getattr(params, "context_scope", "") or "").strip().lower() == "task_local":
        return False
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict) or state.get(_TASK_PROGRESS_NUDGE_STATE_KEY):
        return False
    task_id = durable_task_id(params)
    if not task_id:
        return False
    from ...conversation.runtime import ledger_open_progress_item_count

    open_count = ledger_open_progress_item_count(agent, task_id)
    if open_count <= 0:
        return False
    nudge = "\n".join(
        [
            _TASK_PROGRESS_NUDGE_TAG,
            "持久 task_progress 仍有未关闭项；这是一次软核对提醒，不是完成裁决。",
            f"open_count={open_count}",
            f"tool_round_count={max(0, int(tool_rounds or 0))}",
            "在给最终答复前先调用 task_progress read 核对一次：",
            "- 若仍有工作，继续执行；",
            "- 若产物已经真实完成，用 task_progress update 写入实际 evidence 后再答复；",
            "- 工具失败就按失败处理，不能声称清单已更新。",
            "本提醒只出现一次；普通任务的最终决定仍由模型作出。",
        ]
    )
    params.runtime_injections.append(nudge)
    state[_TASK_PROGRESS_NUDGE_STATE_KEY] = {
        "status": "pending",
        "text": nudge,
    }
    return True


# LLM: consume the exact injected reminder after one accepted provider response so retries and
# subsequent tool rounds cannot accumulate duplicate completion guidance.
# 函数用途: 模型成功读到一次核对提醒后立即移除它，避免后续每一轮重复提示。
def consume_task_progress_completion_nudge(params: ToolLoopExecuteParams) -> None:
    """Remove the one-shot reminder after one accepted model response."""

    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    marker = state.get(_TASK_PROGRESS_NUDGE_STATE_KEY)
    if not isinstance(marker, dict) or marker.get("status") != "pending":
        return
    text = str(marker.get("text") or "")
    injections = getattr(params, "runtime_injections", None)
    if text and isinstance(injections, list):
        try:
            injections.remove(text)
        except ValueError:
            pass
    state[_TASK_PROGRESS_NUDGE_STATE_KEY] = {"status": "consumed"}


def _interim_reply_facts(
    agent: object,
    params: ToolLoopExecuteParams,
    *,
    tool_rounds: int,
    delegated: dict[str, object] | None = None,
) -> dict[str, object]:
    current_request = str(params.root_user_prompt or params.user_prompt or "").strip()
    facts: dict[str, object] = {
        "reply_is_interim": True,
        "task_continues_without_more_user_input": True,
        "current_user_request": _bounded_reply_fact_text(
            current_request,
            _MAX_WAIT_REPLY_REQUEST_CHARS,
        ),
        "completed_action_count": len(list(params.executed_tools or [])),
        "tool_round_count": max(0, int(tool_rounds or 0)),
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
    delegated = delegated or _delegated_work_facts(agent, params)
    if delegated:
        facts["delegated_work"] = delegated
    return facts


def _delegated_work_facts(agent: object, params: ToolLoopExecuteParams) -> dict[str, object]:
    task_id = durable_task_id(params)
    if not task_id:
        return {}
    try:
        from ...subagents.models import normalize_task_status

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
    status_counts: dict[str, int] = {}
    for run in runs:
        try:
            status = normalize_task_status(getattr(run, "status", ""))
        except ValueError:
            status = "UNKNOWN"
        status_counts[status] = status_counts.get(status, 0) + 1
    return {"total": len(runs), "status_counts": dict(sorted(status_counts.items()))}


def _delegated_work_has_open_runs(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    counts = value.get("status_counts")
    if not isinstance(counts, dict):
        return False
    from ...subagents.models import SUBAGENT_ENDED_STATUSES

    for status, raw_count in counts.items():
        try:
            count = int(raw_count or 0)
        except (TypeError, ValueError):
            return True
        if count > 0 and str(status or "") not in SUBAGENT_ENDED_STATUSES:
            return True
    return False


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
