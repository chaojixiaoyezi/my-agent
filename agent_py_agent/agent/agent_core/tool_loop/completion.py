from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import ClassVar

from ...backends import ModelResponse
from ...conversation.active_turn_input import active_turn_user_input_texts
from ...conversation.authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    CONVERSATION_BACKGROUND_EVENT_REASON_ATTR,
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    CONVERSATION_TRANSIENT_WORKSPACE_ATTR,
    CONVERSATION_TURN_REQUEST_ID_ATTR,
    CONVERSATION_WORK_KIND_ATTR,
    CONVERSATION_WORK_NAME_ATTR,
)
from ...tooling.operation_verification import (
    build_operation_verification,
    incomplete_final_mutation_facts,
    post_failure_workspace_mutation_followup_facts,
    post_failure_workspace_mutation_followup_signature,
    public_operation_verification,
)
from .._runtime_params import ToolLoopExecuteParams
from ..runtime.task_identity import durable_task_id
from ..subagent.progress_closeout import subagent_progress_closeout_response
from .background_liveness import is_wake_capable_source
from .natural_user_reply import queue_natural_user_reply
from .round_execution import subagent_output_json_response

_MAX_WAIT_REPLY_REQUEST_CHARS = 4000
_MAX_WAIT_REPLY_GUIDANCE_CHARS = 1200
_MAX_WAIT_REPLY_GUIDANCE_ITEMS = 5
_POST_FAILURE_MUTATION_FOLLOWUP_SIGNATURE_KEY = (
    "post_failure_workspace_mutation_followup_signature"
)
_COMPLETION_CONFLICT_REPAIR_STATE_KEY = "completion_conflict_repair"
_MAX_COMPLETION_CONFLICT_REPAIRS = 2
_MAX_COMPLETION_CONFLICT_DRAFT_CHARS = 1200
_REPAIRABLE_COMPLETION_CONFLICT_STATUSES = frozenset({"failed", "not_started"})


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
    if transition_response := _context_refresh_transition_response(request):
        return transition_response
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
    # 工具轮后模型正文为空 ≠ 收口信号:长期助手/会话运行时/终端应用/通道运行时 四家参考产品
    # 都是"工具→结果→继续采样"直到模型主动输出无工具调用的终态正文(参考调研 2026-08-07)。
    # 真机铁证(2026-08-07, scrapy/celery 复刻):DeepSeek 经 工具运行时 网关工具轮后空正文
    # 是在准备下一步工具调用,旧逻辑在此强制 queue 表达轮收口 → 每请求只调 1-2 个工具,
    # 长任务推进极慢。现在返回 None,主循环带着工具结果继续;模型"空正文无工具"的静默收口
    # 由 response_decision 的 长期助手 式 bounded nudge 兜底(参考 长期助手 "empty response"
    # 塞用户消息要求继续),真正无产出时走诚实 USER_REPLY_UNAVAILABLE。
    return None


def _context_refresh_transition_response(
    request: ToolRoundCompletionRequest,
) -> ModelResponse | None:
    if str(getattr(request.params, "context_scope", "") or "") != "task_local":
        return None
    state = getattr(request.params, "live_archive_state", None)
    if not isinstance(state, dict):
        return None
    transition = state.pop("pending_runtime_transition", None)
    if not isinstance(transition, dict):
        return None
    if (
        str(transition.get("kind") or "") != "context_refresh"
        or str(transition.get("resume") or "") != "next_durable_slice"
        or not str(transition.get("reason") or "").strip()
    ):
        return None
    payload = {
        "status": "PENDING",
        "summary": ("工具已提交耐久状态更新；当前工作片已结束，下一工作片从最新规范状态继续。"),
    }
    return ModelResponse(
        text=(f"[SUBAGENT_RESULT]\n{json.dumps(payload, ensure_ascii=False)}\n[/SUBAGENT_RESULT]"),
        backend=request.response.backend,
    )


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
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    if str(attrs.get(CONVERSATION_BACKGROUND_EVENT_REASON_ATTR) or "").strip():
        # One typed background event owns this turn's user-facing reply. Open
        # descendants remain visible in their durable ledger, but their generic
        # lifecycle presentation must not replace the exact event report.
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


# LLM: A named Audit turn may end its current model call while its typed duration or durable
# source ledger remains open. Discard the model draft and enter the same presentation-only
# phase used by other interim lifecycle states; never infer completion from prose.
# 函数用途: 命名 Audit 尚未到期或仍有来源欠账时，用结构化事实生成阶段回复并保持任务继续。
def queue_interim_reply_for_active_named_work(
    agent: object,
    params: ToolLoopExecuteParams,
    *,
    tool_rounds: int,
) -> bool:
    if str(getattr(params, "context_scope", "") or "") == "task_local":
        return False
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    if attrs.get(CONVERSATION_AUDIT_PREPARE_ATTR) is True:
        # A prepare command owns one ordinary foreground turn.  An already
        # active Audit may coexist with that turn, but its background lifecycle
        # must not replace the prepare result. ``queue_reply_for_audit_prepare``
        # below owns the exact pending/published presentation facts.
        return False
    if str(attrs.get(CONVERSATION_BACKGROUND_EVENT_REASON_ATTR) or "").strip():
        # The model is already answering one exact typed event. Replacing that
        # answer with a generic "Audit is active" presentation round discards
        # the event's authoritative facts.
        return False
    named_work = _active_named_audit_facts(agent, params)
    if not named_work:
        return False
    facts = _interim_reply_facts(
        agent,
        params,
        tool_rounds=tool_rounds,
    )
    continues = named_work.get("continues_without_more_user_input") is True
    facts["reply_is_interim"] = continues
    facts["task_continues_without_more_user_input"] = continues
    facts["named_work"] = named_work
    queue_natural_user_reply(
        params,
        kind="named_work_active" if continues else "named_work_incomplete",
        facts=facts,
    )
    return True


# LLM: Tool-round exhaustion is a typed unfinished state. The tool-bearing draft is never
# delivered; a no-tools presentation turn writes one honest interim response from runtime facts.
# 函数用途: 根代理达到工具轮上限时登记阶段回复，避免模型把未清账工作口头说成已完成。
def queue_interim_reply_for_tool_round_limit(
    agent: object,
    params: ToolLoopExecuteParams,
    *,
    tool_rounds: int,
) -> bool:
    if str(getattr(params, "context_scope", "") or "") == "task_local" or not isinstance(
        getattr(params, "live_archive_state", None), dict
    ):
        return False
    facts = _interim_reply_facts(
        agent,
        params,
        tool_rounds=tool_rounds,
    )
    facts["runtime_limit"] = {
        "status": "unfinished",
        "reason": "TOOL_ROUND_LIMIT_REACHED",
        "limit_reached": True,
    }
    if named_work := _active_named_audit_facts(agent, params):
        facts["named_work"] = named_work
    queue_natural_user_reply(
        params,
        kind="tool_round_limit",
        facts=facts,
    )
    return True


# LLM: Mirror 会话运行时 Stop-hook continuation semantics for a repairable completion
# conflict: keep the same active turn and tool surface, return typed facts plus
# the rejected draft to the model, and only enter the no-tools fail-closed reply
# after a bounded retry budget. Unknown/cancelled/incomplete/unverified effects
# never regain general tool authority here. No model prose is parsed.
# 函数用途: 末尾副作用明确失败或未执行时，先让同一主模型带工具返工；不确定副作用或返工耗尽才安全收口。
def queue_reply_for_incomplete_final_mutation(
    agent: object,
    params: ToolLoopExecuteParams,
    *,
    response: ModelResponse,
    tool_rounds: int,
) -> bool:
    if str(getattr(params, "context_scope", "") or "") == "task_local":
        return False
    operation_facts = incomplete_final_mutation_facts(
        agent,
        list(getattr(params, "archive_tool_calls", None) or []),
    )
    if not operation_facts:
        return False
    latest = operation_facts.get("latest_mutating_operation")
    latest = latest if isinstance(latest, dict) else {}
    latest_status = str(latest.get("status") or "unverified")
    if latest_status in _REPAIRABLE_COMPLETION_CONFLICT_STATUSES:
        if _queue_completion_conflict_repair(
            params,
            response=response,
            operation_facts=operation_facts,
        ):
            return True
    _queue_completion_conflict_fallback(
        agent,
        params,
        response=response,
        operation_facts=operation_facts,
        tool_rounds=tool_rounds,
    )
    return True


# LLM: The repair counter is host-owned state for this active turn, not a model
# claim and not a per-call retry switch. A new failed call therefore consumes
# the same bounded budget instead of rearming an unbounded completion loop.
# 函数用途: 把一次收口冲突写成 会话运行时 Stop-hook 风格的续作提示，并保留原工具能力让模型修复和复验。
def _queue_completion_conflict_repair(
    params: ToolLoopExecuteParams,
    *,
    response: ModelResponse,
    operation_facts: dict[str, object],
) -> bool:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return False
    repair_state = state.get(_COMPLETION_CONFLICT_REPAIR_STATE_KEY)
    repair_state = repair_state if isinstance(repair_state, dict) else {}
    attempts = _nonnegative_int(repair_state.get("attempts"))
    if attempts >= _MAX_COMPLETION_CONFLICT_REPAIRS:
        repair_state["exhausted"] = True
        state[_COMPLETION_CONFLICT_REPAIR_STATE_KEY] = repair_state
        return False
    attempt = attempts + 1
    draft = _bounded_reply_fact_text(
        str(getattr(response, "text", "") or ""),
        _MAX_COMPLETION_CONFLICT_DRAFT_CHARS,
    )
    latest = operation_facts.get("latest_mutating_operation")
    verification = operation_facts.get("operation_verification")
    envelope: dict[str, object] = {
        "schema": "completion_conflict.v1",
        "conflict": "final_draft_after_unsucceeded_mutation",
        "repair_attempt": attempt,
        "max_repair_attempts": _MAX_COMPLETION_CONFLICT_REPAIRS,
        "latest_mutating_operation": dict(latest) if isinstance(latest, dict) else {},
        "operation_verification": (
            dict(verification) if isinstance(verification, dict) else {}
        ),
    }
    if draft:
        envelope["rejected_completion_draft"] = draft
    latest_envelope = envelope["latest_mutating_operation"]
    repair_state = {
        "attempts": attempt,
        "exhausted": False,
        "last_model_turn_id": str(state.get("_current_model_turn_id") or ""),
        "latest_status": str(
            latest_envelope.get("status")
            if isinstance(latest_envelope, dict)
            else "unverified"
        ),
    }
    state[_COMPLETION_CONFLICT_REPAIR_STATE_KEY] = repair_state
    params.tool_context.append(
        "# Completion conflict repair\n"
        "```json\n"
        + json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n```\n"
        "上一份最终草稿与本轮权威工具终态冲突，已暂缓交付。你仍处于同一个 active turn，"
        "并继续拥有原来的工具。请先根据失败阶段、错误码和真实工具输出定位问题，选择安全的修复或替代方法，"
        "再用新的结构化成功回执复验；不要只重复完成结论，也不要盲目重放可能产生副作用的命令。"
        "如果确实无法安全推进，请基于真实证据明确说明阻碍、已尝试动作和需要用户补充的条件。"
    )
    return True


# LLM: This is the last safety boundary after repair exhaustion or an uncertain
# effect. It may use the no-tools presentation phase because automatic tool use
# is no longer safe; the original draft remains non-authoritative context only.
# 函数用途: 返工耗尽或副作用不确定时，用结构化事实生成未完成说明并停止继续自动执行。
def _queue_completion_conflict_fallback(
    agent: object,
    params: ToolLoopExecuteParams,
    *,
    response: ModelResponse,
    operation_facts: dict[str, object],
    tool_rounds: int,
) -> None:
    facts = _interim_reply_facts(agent, params, tool_rounds=tool_rounds)
    facts["task_continues_without_more_user_input"] = False
    facts.update(operation_facts)
    queue_natural_user_reply(
        params,
        kind="operation_incomplete",
        facts=facts,
        draft=str(getattr(response, "text", "") or ""),
    )


# LLM: Repair counters accept only finite non-negative integers; malformed live
# state must fail to zero instead of expanding the continuation budget.
# 函数用途: 安全读取收口返工次数，避免损坏状态造成无限续作。
def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: 该入口按 durable verification event 去重软提醒：read/search 不能清除 stale，
# 同一测试周期只提醒一次；新测试后再修改才形成新周期。它不解析草稿，也不设置 blocked/unfinished。
# 函数用途: 在真实验证过期时丢弃一次过早收口草稿，给模型继续验证或明确解释的机会。
def queue_followup_after_post_failure_workspace_mutation(
    agent: object,
    params: ToolLoopExecuteParams,
) -> bool:
    if str(getattr(params, "context_scope", "") or "") == "task_local":
        return False
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return False
    facts = post_failure_workspace_mutation_followup_facts(
        agent,
        list(getattr(params, "archive_tool_calls", None) or []),
    )
    if not facts:
        return False
    signature = post_failure_workspace_mutation_followup_signature(facts)
    if state.get(_POST_FAILURE_MUTATION_FOLLOWUP_SIGNATURE_KEY) == signature:
        return False
    state[_POST_FAILURE_MUTATION_FOLLOWUP_SIGNATURE_KEY] = signature
    params.tool_context.append(
        "# Post-failure workspace follow-up\n"
        "```json\n"
        + json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n```\n"
        "这是当前请求的结构化工具与验证账本事实，不是完成硬门。最近一次有效验证之后工作区又发生了修改，"
        "且普通读取或搜索不能证明修改后的版本可用。请依据用户目标自主决定：若需要复核，就调用合适的检查工具并根据真实结果继续修复；"
        "若客观上无需复核，就直接给出明确最终说明。不要只描述下一步准备做什么。"
    )
    return True


# LLM: A prepare turn can inspect, test, and discuss without publishing a new
# effective Audit revision.  A successful publish keeps the final answer from
# that same tool-bearing model turn, as 会话运行时/长期助手 do.  A still-pending
# prepare must be presented from the durable state so prose cannot falsely
# claim publication; that presentation retains the current turn's bounded
# tool history (see ``natural_user_reply_model_params``), so it cannot mistake
# its reduced reply surface for evidence that no tool execution occurred.
# 函数用途: 发布成功时保留同轮回复；未发布时用耐久状态纠正但保留本轮工具证据。
def queue_reply_for_audit_prepare(
    agent: object,
    params: ToolLoopExecuteParams,
    *,
    response: ModelResponse,
    tool_rounds: int,
) -> bool:
    if str(getattr(params, "context_scope", "") or "") == "task_local":
        return False
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    if (
        attrs.get(CONVERSATION_AUDIT_PREPARE_ATTR) is not True
        or attrs.get(CONVERSATION_TRANSIENT_WORKSPACE_ATTR) is not True
        or str(attrs.get(CONVERSATION_WORK_KIND_ATTR) or "").strip().lower() != "audit"
    ):
        return False
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    task_id = str(attrs.get("conversation_task_id") or "").strip()
    work_name = str(attrs.get(CONVERSATION_WORK_NAME_ATTR) or "").strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or not task_id or not work_name or store is None:
        return False
    link = _matching_audit_prepare_link(store, thread_id, task_id, work_name)
    if link is None:
        return False
    prepare_request_id = str(attrs.get(CONVERSATION_TURN_REQUEST_ID_ATTR) or "").strip()
    pending = bool(str(getattr(link, "pending_prompt", "") or "").strip())
    published = bool(
        prepare_request_id
        and not pending
        and str(getattr(link, "effective_prepare_request_id", "") or "").strip()
        == prepare_request_id
    )
    if not pending and not published:
        return False
    if published and str(getattr(response, "text", "") or "").strip():
        response.runtime_status = "ok"
        response.runtime_reason = "AUDIT_PREPARE_RESULT"
        response.runtime_source = "conversation_task"
        return False
    publish_facts = _current_audit_publish_facts(params, prepare_request_id)
    named_work = _audit_prepare_named_work(
        link,
        work_name=work_name,
        pending=pending,
        published=published,
        publish_facts=publish_facts,
    )
    facts = _interim_reply_facts(agent, params, tool_rounds=tool_rounds)
    # ``operation_verification`` is an audit trail of every mutating attempt,
    # not the authoritative outcome of this named Audit revision.  A model may
    # legitimately repair an invalid probe call and then publish a verified
    # revision; presenting both the stale failed attempt and the durable
    # published outcome as peer facts makes the wording phase misreport the
    # repaired attempt as the final state.  Keep the full attempt ledger on the
    # response/channel metadata, while this presentation phase reads the one
    # typed outcome already projected below from the durable task link and the
    # exact publish envelope.
    facts.pop("operation_verification", None)
    facts.update(
        {
            "reply_is_interim": False,
            "task_continues_without_more_user_input": False,
            "named_work": named_work,
        }
    )
    queue_natural_user_reply(
        params,
        kind="audit_prepare_pending" if pending else "audit_prepare_result",
        facts=facts,
        # The ordinary tool-loop closeout is only an unverified presentation
        # draft.  Audit publication already has one authoritative source: the
        # durable link plus this exact turn's typed publish envelope.  Feeding
        # the draft back into the no-tools presentation turn gives providers a
        # second, conflicting source and can make them repeat a false
        # "published" claim even while the durable state is still pending.
        draft="",
    )
    return True


def _matching_audit_prepare_link(
    store: object,
    thread_id: str,
    task_id: str,
    work_name: str,
) -> object | None:
    try:
        links, errors = store.task_links_report(thread_id)
    except Exception:
        return None
    if errors:
        return None
    return next(
        (
            item
            for item in links
            if str(getattr(item, "task_id", "") or "").strip() == task_id
            and str(getattr(item, "work_kind", "") or "").strip().lower() == "audit"
            and str(getattr(item, "work_name", "") or "").strip() == work_name
        ),
        None,
    )


def _audit_prepare_named_work(
    link: object,
    *,
    work_name: str,
    pending: bool,
    published: bool,
    publish_facts: dict[str, object],
) -> dict[str, object]:
    source_bindings = tuple(getattr(link, "effective_source_bindings", ()) or ())
    named_work: dict[str, object] = {
        "work_kind": "audit",
        "work_name": work_name,
        "status": str(getattr(link, "status", "") or "preparing"),
        "update_applied": published,
        "effective_revision": max(
            0,
            int(getattr(link, "effective_revision", 0) or 0),
        ),
        "effective_source_count": len(source_bindings),
        "effective_source_ids": [
            str(item.get("source_id") or "")
            for item in source_bindings
            if isinstance(item, dict) and str(item.get("source_id") or "").strip()
        ],
    }
    if pending:
        named_work.update(
            {
                "pending_requirement_preserved": True,
                "source_binding_change_applied": False,
                "applied_source_probe_count": 0,
                "applied_source_ids": [],
                "publication_validation_status": "pending",
                "source_access_verified": False,
            }
        )
        return named_work
    applied_source_probe_count = max(
        0,
        int(publish_facts.get("applied_source_probe_count") or 0),
    )
    named_work.update(
        {
            "source_binding_change_applied": bool(
                publish_facts.get("source_binding_change_applied") is True
            ),
            "applied_source_probe_count": applied_source_probe_count,
            "applied_source_ids": list(publish_facts.get("applied_source_ids") or []),
            "publication_validation_status": str(
                publish_facts.get("validation_status") or "not_required"
            ),
            # A source can only enter the durable binding set through a
            # successful current-Audit watch probe.
            "source_access_verified": bool(applied_source_probe_count > 0),
        }
    )
    return named_work


def _current_audit_publish_facts(
    params: ToolLoopExecuteParams,
    prepare_request_id: str,
) -> dict[str, object]:
    """Return host-owned source-change facts from this exact prepare turn."""

    applied_source_ids: list[str] = []
    source_probe_refs: set[str] = set()
    validation_status = ""
    for record in list(getattr(params, "archive_tool_calls", None) or []):
        if not isinstance(record, dict) or record.get("ok") is not True:
            continue
        if str(record.get("tool") or "") != "publish_audit_update":
            continue
        record_request_id = str(record.get("request_id") or "").strip()
        if prepare_request_id and record_request_id != prepare_request_id:
            continue
        parameters = record.get("parameters")
        parameters = parameters if isinstance(parameters, dict) else {}
        for ref in parameters.get("source_probe_refs") or []:
            text = str(ref or "").strip()
            if text:
                source_probe_refs.add(text)
        envelope = record.get("tool_result_envelope")
        envelope = envelope if isinstance(envelope, dict) else {}
        published = envelope.get("audit_publish")
        published = published if isinstance(published, dict) else {}
        published_validation = str(published.get("validation_status") or "").strip()
        if published_validation:
            validation_status = published_validation
        for source_id in published.get("applied_source_ids") or []:
            text = str(source_id or "").strip()
            if text and text not in applied_source_ids:
                applied_source_ids.append(text)
    return {
        "source_binding_change_applied": bool(source_probe_refs),
        "applied_source_probe_count": len(source_probe_refs),
        "applied_source_ids": applied_source_ids,
        "validation_status": validation_status,
    }


def _active_named_audit_facts(
    agent: object,
    params: ToolLoopExecuteParams,
) -> dict[str, object]:
    task_id = _delegated_work_task_id(params)
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    if not task_id or not thread_id:
        return {}
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return {}
    try:
        if callable(getattr(store, "task_links_report", None)):
            links, errors = store.task_links_report(thread_id)
            if errors:
                return (
                    {
                        "task_id": task_id,
                        "work_kind": "audit",
                        "state_available": False,
                        "continues_without_more_user_input": True,
                    }
                    if str(attrs.get(CONVERSATION_WORK_KIND_ATTR) or "").lower() == "audit"
                    else {}
                )
        else:
            links = store.task_links(thread_id)
    except Exception:
        return (
            {
                "task_id": task_id,
                "work_kind": "audit",
                "state_available": False,
                "continues_without_more_user_input": True,
            }
            if str(attrs.get(CONVERSATION_WORK_KIND_ATTR) or "").lower() == "audit"
            else {}
        )
    link = next(
        (
            item
            for item in links
            if str(getattr(item, "task_id", "") or "").strip() == task_id
            and str(getattr(item, "status", "") or "").strip().lower() == "active"
            and str(getattr(item, "work_kind", "") or "").strip().lower() == "audit"
        ),
        None,
    )
    if link is None:
        return {}
    now = time.time()
    try:
        expires_at = float(getattr(link, "expires_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        expires_at = 0.0
    from ...ingestion.audit_state import (
        audit_task_activation_facts,
        audit_task_source_facts,
        audit_task_summary_facts,
        task_has_incomplete_watch,
    )

    sources_incomplete = task_has_incomplete_watch(agent, task_id, now=now)
    activation = audit_task_activation_facts(agent, link, now=now)
    activation_ready = activation.get("ready") is True
    if expires_at <= now and not sources_incomplete and activation_ready:
        return {}
    return {
        "task_id": task_id,
        "work_kind": "audit",
        "work_name": str(getattr(link, "work_name", "") or ""),
        "state_available": True,
        "expires_at": expires_at or None,
        "collection_window_active": bool(expires_at > now),
        "sources_incomplete": sources_incomplete,
        "activation": activation,
        "sources": audit_task_source_facts(agent, task_id, now=now),
        "summary": audit_task_summary_facts(agent, task_id, now=now),
        "continues_without_more_user_input": bool(expires_at > now or sources_incomplete),
    }


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
    verification = public_operation_verification(
        build_operation_verification(
            agent,
            list(getattr(params, "archive_tool_calls", None) or []),
        )
    )
    operation_count = max(0, int(verification.get("operation_count") or 0))
    verification_counts = verification.get("counts")
    verification_counts = verification_counts if isinstance(verification_counts, dict) else {}
    succeeded_operation_count = max(
        0,
        int(verification_counts.get("succeeded") or 0),
    )
    failed_operation_count = max(
        0,
        int(verification_counts.get("failed") or 0),
    )
    executed_tool_names = sorted(
        {
            str(item or "").strip()
            for item in list(getattr(params, "executed_tools", None) or [])
            if str(item or "").strip()
        }
    )
    facts["turn_execution"] = {
        # The follow-up model call only writes the user-facing sentence.  It
        # intentionally receives no executable schemas, so that local detail
        # must never be confused with the tool access or work already observed
        # in the main turn.
        "presentation_only": True,
        "tool_execution_observed": bool(executed_tool_names or operation_count),
        "executed_tool_count": len(executed_tool_names),
        "successful_operation_count": succeeded_operation_count,
        "failed_operation_count": failed_operation_count,
    }
    if operation_count > 0:
        facts["operation_verification"] = verification
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
    task_id = _delegated_work_task_id(params)
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


def _delegated_work_task_id(params: ToolLoopExecuteParams) -> str:
    """Resolve child ownership for this turn, not merely its inherited cwd.

    A conversation may keep one sticky workspace while detached named work
    continues in the background.  The workspace's task id is therefore not
    proof that an unrelated foreground chat turn owns those children.  Only an
    exact request/task match or the typed per-turn activation flag may inherit
    the durable task's delegated runs.
    """
    attrs = getattr(params, "task_attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    bound_task_id = str(attrs.get("conversation_task_id") or "").strip()
    request_task_id = str(getattr(params, "task_id", "") or "").strip()
    if not bound_task_id:
        return request_task_id
    if request_task_id == bound_task_id:
        return bound_task_id
    if attrs.get(CONVERSATION_TASK_TURN_ACTIVE_ATTR) is True:
        return durable_task_id(params)
    return ""


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


__all__ = [
    "ToolRoundCompletionRequest",
    "completion_response_after_tool_round",
    "queue_followup_after_post_failure_workspace_mutation",
    "queue_interim_reply_for_active_named_work",
    "queue_interim_reply_for_open_subagents",
    "queue_interim_reply_for_tool_round_limit",
    "queue_reply_for_incomplete_final_mutation",
    "queue_reply_for_audit_prepare",
]
