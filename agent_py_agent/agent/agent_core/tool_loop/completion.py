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
    public_operation_verification,
)
from .._runtime_params import ToolLoopExecuteParams
from ..runtime.guidance import (
    active_turn_user_reply_required,
    satisfy_active_turn_user_reply,
)
from ..runtime.task_identity import durable_task_id
from .natural_user_reply import queue_natural_user_reply

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


# LLM: A task-local recursive create ends that child execution slice, but a root
# turn receives the create result in the same active turn so it
# may immediately send guidance/cancel before choosing to wait. Do not queue a
# presentation receipt merely because create_subagents succeeded.
# 函数用途: 工具轮结束后处理 Compact 转换；孙代理创建后让子代理等待，主代理则继续同一回合以便立刻追加消息。
def completion_response_after_tool_round(
    request: ToolRoundCompletionRequest,
) -> ModelResponse | None:
    if transition_response := _context_refresh_transition_response(request):
        return transition_response
    visible_text = bool(str(getattr(request.response, "text", "") or "").strip())
    if visible_text:
        # Tool start is a real public commentary boundary in both Gateway and
        # child transcript sinks, so this model-authored segment satisfies the
        # consumed steer even though the task continues to execute tools.
        satisfy_active_turn_user_reply(request.params)
    successful_tools = list(request.params.executed_tools or [])[request.before_executed_count :]
    if "create_subagents" in successful_tools:
        if active_turn_user_reply_required(request.params):
            # A user steer that immediately caused another child spawn still
            # needs one ordinary reply. Continue the same bounded model loop;
            # do not settle the parent wait with an empty assistant message.
            return None
        if wait_response := task_local_wait_response_for_open_subagents(
            request.agent,
            request.params,
            response=request.response,
        ):
            return wait_response
        # 会话运行时's spawn_agent result is an ordinary tool result: the parent
        # model gets another sample in the same active turn and can immediately
        # call send_message.  Root create_subagents must preserve that control
        # opportunity; the generic open-child receipt is queued only if the
        # model later tries to finish while a direct child is still active.
    # 工具轮后模型正文为空 ≠ 收口信号:长期助手/会话运行时/终端应用/通道运行时 四家参考产品
    # 都是"工具→结果→继续采样"直到模型主动输出无工具调用的终态正文(参考调研 2026-08-07)。
    # 真机铁证(2026-08-07, scrapy/celery 复刻):DeepSeek 经 工具运行时 网关工具轮后空正文
    # 是在准备下一步工具调用,旧逻辑在此强制 queue 表达轮收口 → 每请求只调 1-2 个工具,
    # 长任务推进极慢。现在返回 None,主循环带着工具结果继续;模型"空正文无工具"的静默收口
    # 由 response_decision 的 长期助手 式 bounded nudge 兜底(参考 长期助手 "empty response"
    # 塞用户消息要求继续),真正无产出时走诚实 USER_REPLY_UNAVAILABLE。
    return None


# LLM: A task-local parent with active direct children cannot close as DONE.
# The host stores an exact-id wait marker and returns an interrupted slice; any
# model-authored text from this slice must survive as the visible interim reply.
# The next model turn is opened only by a child event or explicit user guidance.
# 函数用途: 子代理还有直属孩子运行时，保留本轮回复后安全结束工作片并等事件。
def task_local_wait_response_for_open_subagents(
    agent: object,
    params: ToolLoopExecuteParams,
    *,
    response: object | None = None,
) -> ModelResponse | None:
    if str(getattr(params, "context_scope", "") or "") != "task_local":
        return None
    from ...subagents.direct_parent_lifecycle import (
        mark_parent_waiting_for_direct_children,
    )
    from ..runner.context import current_subagent_run_id

    parent_run_id = current_subagent_run_id(agent) or str(getattr(params, "run_id", "") or "")
    waiting = mark_parent_waiting_for_direct_children(
        getattr(agent, "subagents", None),
        parent_run_id,
    )
    if not waiting:
        return None
    backend = str(
        getattr(response, "backend", "")
        or getattr(getattr(agent, "backend", None), "name", "")
        or "tool_loop"
    )
    return ModelResponse(
        text=str(getattr(response, "text", "") or ""),
        backend=backend,
        runtime_status="unfinished",
        runtime_reason="SUBAGENTS_ACTIVE",
        runtime_source="subagent_lifecycle",
        turn_end_reason="interrupted",
    )


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
    return ModelResponse(
        text="工具已提交耐久状态更新；当前工作片已结束，下一工作片从最新状态继续。",
        backend=request.response.backend,
        runtime_status="unfinished",
        runtime_reason="CONTEXT_REFRESH",
        runtime_source="tool_loop",
        turn_end_reason="max-tokens",
    )


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


# LLM: 阶段回执里的子代理事实必须来自持久化 run 的 parent/root/depth/status；
#   用户原始要求和模型先前文字都不能补造“协调代理/孙代理已经创建”等拓扑。
# 函数用途: 汇总当前任务的子代理状态与真实父子关系，供无工具自然回执如实描述。
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
    return {
        "schema": "delegated_work.v2",
        "authority": "subagent_store",
        "total": len(runs),
        "status_counts": dict(sorted(status_counts.items())),
        "topology": _delegated_work_topology(task_id, runs),
    }


# LLM: topology 是 会话运行时 parent_thread_id/depth 同类的结构化投影；节点截断只影响
#   展示细节，计数必须覆盖完整 run 集，且未知父级不能静默算作直属或后代。
# 函数用途: 计算直属子代理、后代代理和未知父级数量，并提供有限节点样本给回复模型核对。
def _delegated_work_topology(task_id: str, runs: list[object]) -> dict[str, object]:
    run_ids = {
        str(getattr(run, "id", "") or "").strip()
        for run in runs
        if str(getattr(run, "id", "") or "").strip()
    }
    direct_count = 0
    descendant_count = 0
    unknown_parent_count = 0
    max_depth = 0
    rows: list[dict[str, object]] = []
    for run in runs:
        run_id = str(getattr(run, "id", "") or "").strip()
        parent_id = str(getattr(run, "parent_id", "") or "").strip()
        try:
            depth = max(0, int(getattr(run, "depth", 0) or 0))
        except (TypeError, ValueError):
            depth = 0
        max_depth = max(max_depth, depth)
        if parent_id == task_id:
            parent_scope = "root"
            direct_count += 1
        elif parent_id in run_ids:
            parent_scope = "delegated_agent"
            descendant_count += 1
        else:
            parent_scope = "unknown"
            unknown_parent_count += 1
        if len(rows) < 32:
            rows.append(
                {
                    "run_id": run_id,
                    "parent_run_id": parent_id,
                    "parent_scope": parent_scope,
                    "depth": depth,
                    "agent_name": str(getattr(run, "agent_name", "") or "").strip(),
                    "role": str(getattr(run, "role", "") or "").strip(),
                    "status": str(getattr(run, "status", "") or "").strip(),
                }
            )
    return {
        "root_run_id": task_id,
        "direct_child_count": direct_count,
        "descendant_count": descendant_count,
        "unknown_parent_count": unknown_parent_count,
        "max_depth": max_depth,
        "nested_delegation_observed": descendant_count > 0,
        "nodes": rows,
        "nodes_truncated": len(rows) < len(runs),
    }


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
    "queue_interim_reply_for_active_named_work",
    "queue_interim_reply_for_open_subagents",
    "queue_interim_reply_for_tool_round_limit",
    "queue_reply_for_audit_prepare",
    "task_local_wait_response_for_open_subagents",
]
