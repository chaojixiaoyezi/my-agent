from __future__ import annotations

import json
from dataclasses import replace

from ...backends import ModelResponse
from ...backends.tool_ir import UserTurn
from ...conversation.user_visible_text import (
    contains_internal_protocol,
    sanitize_user_visible_text,
)
from .._runtime_params import ToolLoopExecuteParams
from ..native_tool_protocol import native_tool_use_active

_STATE_KEY = "_pending_natural_user_reply"
_MAX_GENERATION_ATTEMPTS = 2


# LLM: pending state 只保存结构化事实、可丢弃草稿和内部完成信封；不能在这里预写用户句子。
# 函数用途: 为下一次同模型无工具短轮登记回复阶段，并保持派工回执不被低优先级更新覆盖。
def queue_natural_user_reply(
    params: ToolLoopExecuteParams,
    *,
    kind: str,
    facts: dict[str, object],
    draft: str = "",
) -> None:
    """Queue one model-written user reply after a structured runtime action."""
    state = _mutable_state(params)
    if state is None:
        return
    current = state.get(_STATE_KEY)
    current = current if isinstance(current, dict) else {}
    current_kind = str(current.get("kind") or "")
    if current_kind == "background_dispatch" and kind != "background_dispatch":
        return
    attempts = int(current.get("attempts") or 0) if current_kind == kind else 0
    payload: dict[str, object] = {
        "kind": str(kind or "background_update"),
        "facts": dict(facts),
        "attempts": attempts,
    }
    if draft.strip():
        payload["draft"] = draft.strip()
    state[_STATE_KEY] = payload


# LLM: 读取 pending 只认本 run 的 live_archive_state，不从 transcript 或模型正文猜回复阶段。
# 函数用途: 返回当前待生成的自然回复事实包；没有登记则返回 None。
def pending_natural_user_reply(params: ToolLoopExecuteParams) -> dict[str, object] | None:
    state = _mutable_state(params)
    if state is None:
        return None
    value = state.get(_STATE_KEY)
    return value if isinstance(value, dict) else None


# LLM: 表达轮保留 system persona 和 owner 的 SOUL/USER/AGENTS，但不再把原任务放在
#   提示词最尾；否则 MiniMax 等模型会把“写一句回执”错当成“重新执行原任务”，
#   并再次吐出工具协议。结构化回复事实必须作为唯一末尾 UserTurn 进入
#   native wire；CacheStructuredPrompt.canonical_user_turn 只是诊断副本，不能代替真实 messages。
# 函数用途: 构造低延迟、零工具、不重做原任务的临时模型参数视图。
def natural_user_reply_model_params(params: ToolLoopExecuteParams) -> ToolLoopExecuteParams:
    """Return a deliberately thin no-tools view for one user-facing model reply."""
    phase = pending_natural_user_reply(params)
    if phase is None:
        return params
    guidance = _reply_guidance(phase)
    kind = str(phase.get("kind") or "")
    preserve_execution_evidence = kind in {
        "audit_prepare_pending",
        "audit_prepare_result",
    }
    return replace(
        params,
        user_prompt=guidance,
        memories=[],
        # isolated 会跳过项目默认的工具/执行规约，仍保留 owner 人格入口；
        # guidance 放在最后的 User Task，所以 text/native 后端都一定看得到。
        runtime_injections=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        # A pending Audit prepare is the one reply phase where the exact
        # current-turn execution history is part of the fact being presented:
        # the model may have probed an interface and written candidate notes
        # without publishing them.  Keep the already windowed transcript/IR
        # visible while still exposing no callable schemas.  Other reply kinds
        # remain deliberately thin so background progress cannot drag tool
        # payloads into a second generation.
        tool_context=(params.tool_context if preserve_execution_evidence else []),
        allowed_tools=[],
        tool_ir_history=_natural_reply_tool_ir_history(
            params,
            guidance=guidance,
            preserve_execution_evidence=preserve_execution_evidence,
        ),
        provider_history_messages=(
            list(params.provider_history_messages)
            if preserve_execution_evidence
            else []
        ),
        conversation_history_seed=(
            params.conversation_history_seed
            if preserve_execution_evidence
            else None
        ),
        delivery_contract=None,
        context_scope="isolated",
        consume_pending_turn_input=False,
    )


# LLM: native 表达轮的 User Task 权威在 IR messages，不在 prompt 的诊断字符串。
#   保留执行证据时必须复制原 IR 再追加回执，不得修改主轮的共享 list。
# 函数用途: 为零工具表达轮构造唯一末尾用户消息，并保持重复构造幂等。
def _natural_reply_tool_ir_history(
    params: ToolLoopExecuteParams,
    *,
    guidance: str,
    preserve_execution_evidence: bool,
) -> list[object]:
    if not native_tool_use_active(params):
        return list(params.tool_ir_history) if preserve_execution_evidence else []
    canonical = f"# User Task\n{str(guidance or '')}"
    history = list(params.tool_ir_history) if preserve_execution_evidence else []
    history = [
        item
        for item in history
        if not (isinstance(item, UserTurn) and item.text == canonical)
    ]
    history.append(UserTurn(canonical))
    return history


def discard_pending_natural_user_reply(params: ToolLoopExecuteParams) -> bool:
    """Drop an auxiliary reply made stale by newer active-turn input.

    The control acknowledgement for ``/btw`` is already sent by the control
    path.  Keeping an older dispatch/progress draft would either delay the
    steer or let the presentation-only round consume it.  Dropping only this
    ephemeral phase leaves the durable task, transcript, and guidance intact.
    """
    state = _mutable_state(params)
    if state is None:
        return False
    return state.pop(_STATE_KEY, None) is not None


# LLM: 用户回复出口只校验机器可判定的响应形态；任务是否完成、时间和产物事实全部由
# 结构化运行记录承载，不能再用中英文关键字或正则猜测模型句子的语义。
# 函数用途: 拦运行失败、空回复、真实工具调用和内部协议，不对自然语言内容作状态裁决。
def natural_user_reply_is_acceptable(
    response: object,
    phase: dict[str, object] | None = None,
) -> bool:
    return natural_user_reply_rejection_reason(response, phase) == ""


def natural_user_reply_rejection_reason(
    response: object,
    phase: dict[str, object] | None = None,
) -> str:
    """Return a bounded machine reason for one rejected user-facing draft."""
    del phase
    if str(getattr(response, "runtime_status", "ok") or "ok").strip().lower() != "ok":
        return "model_runtime_status"
    if list(getattr(response, "tool_use_blocks", None) or []):
        return "structured_tool_call"
    text = str(getattr(response, "text", "") or "").strip()
    if not text:
        return "empty_text"
    # internal_protocol 只拒绝「剥离协议信封后没有任何可交付正文」的回复。
    # 真机铁证(2026-08-07, M2.7 表达轮):模型把工具调用降级成正文
    # [TOOL_CALL]...[/TOOL_CALL] 块,剥离后仍剩自然正文(「让我先看下…」);
    # 整条拒绝会让表达轮 25-40% 失败。剥离后非空 → 接受,交付时交付剥离
    # 后正文(与 structured_tool_call 的 salvage 先例同一语义)。
    sanitized = sanitize_user_visible_text(text)
    if sanitized.removed_protocol and not sanitized.content:
        return "internal_protocol"
    return ""


# LLM: 重写次数是本回复阶段的结构化计数，上限后抑制正文，避免坏输出无限耗费 token。
# 函数用途: 记录一次不合格生成并判断是否还能再让同一模型重写一次。
def retry_natural_user_reply(
    params: ToolLoopExecuteParams,
    *,
    rejection_reason: str = "",
) -> bool:
    phase = pending_natural_user_reply(params)
    if phase is None:
        return False
    attempts = max(0, int(phase.get("attempts") or 0)) + 1
    phase["attempts"] = attempts
    if rejection_reason:
        phase["previous_rejection"] = str(rejection_reason)
    return attempts < _MAX_GENERATION_ATTEMPTS


# LLM: 表达轮只用于等待、派工和前台让出等 interim 状态；普通最终回复直接来自主模型 turn。
# 函数用途: 清除 pending 状态并把本次模型短轮归一成最终 ModelResponse。
def finish_natural_user_reply(
    params: ToolLoopExecuteParams,
    response: ModelResponse,
    *,
    accepted: bool,
    rejection_reason: str = "",
) -> ModelResponse:
    state = _mutable_state(params)
    phase = state.pop(_STATE_KEY, None) if state is not None else None
    phase = phase if isinstance(phase, dict) else {}
    kind = str(phase.get("kind") or "background_update")
    runtime_status, runtime_reason, runtime_source = _reply_runtime_state(kind)
    if not accepted:
        # The presentation-only round never offers tools.  Some compatible
        # providers nevertheless attach a structured tool_use block to an
        # otherwise valid natural-language reply.  The first occurrence is
        # still retried above; after the bounded retry, discard that
        # unauthorized machine block and preserve only the sanitized prose
        # (protocol envelopes stripped, same boundary as every user reply).
        # This is a structural decision (no tools were authorized), not a
        # guess based on the wording of the reply.
        sanitized = sanitize_user_visible_text(response.text)
        if rejection_reason == "structured_tool_call" and sanitized.content:
            return replace(
                response,
                text=sanitized.content,
                runtime_status=runtime_status,
                runtime_reason=runtime_reason,
                runtime_source=(
                    runtime_source + "_unauthorized_tools_discarded"
                ),
                tool_use_blocks=[],
            )
        return replace(
            response,
            text="",
            runtime_status=(
                runtime_status
                if runtime_status != "ok"
                else "user_reply_unavailable"
            ),
            runtime_reason=runtime_reason,
            runtime_source=runtime_source,
            tool_use_blocks=[],
        )
    return replace(
        response,
        text=sanitize_user_visible_text(response.text).content,
        runtime_status=runtime_status,
        runtime_reason=runtime_reason,
        runtime_source=runtime_source,
        tool_use_blocks=[],
    )


def _reply_runtime_state(kind: str) -> tuple[str, str, str]:
    if kind == "tool_round_limit":
        return "unfinished", "TOOL_ROUND_LIMIT_REACHED", "tool_loop"
    if kind == "named_work_active":
        return "unfinished", "NAMED_WORK_ACTIVE", "conversation_task"
    if kind in {"named_work_incomplete", "named_work_activation_incomplete"}:
        return "unfinished", "NAMED_WORK_INCOMPLETE", "conversation_task"
    if kind == "operation_incomplete":
        return "unfinished", "OPERATION_INCOMPLETE", "tool_runtime"
    if kind == "audit_prepare_pending":
        return "ok", "AUDIT_PREPARE_PENDING", "conversation_task"
    if kind == "audit_prepare_result":
        return "ok", "AUDIT_PREPARE_RESULT", "conversation_task"
    return "ok", kind, "model_user_reply"


# LLM: The presentation model may translate structured phase facts but cannot
# expose internal field names or invent additional execution/verification work.
# 函数用途: 生成面向普通用户的自然回复约束，避免把底层协议词直接显示出来。
def _reply_guidance(phase: dict[str, object]) -> str:
    payload = {
        "reply_kind": str(phase.get("kind") or "background_update"),
        "facts": phase.get("facts") if isinstance(phase.get("facts"), dict) else {},
        "draft": str(phase.get("draft") or ""),
        "attempt": max(0, int(phase.get("attempts") or 0)) + 1,
        "previous_rejection": str(phase.get("previous_rejection") or ""),
    }
    retry_note = (
        "上一版没有通过用户出口检查；不要复述或包装上一版，只重新写一段纯自然语言回复。"
        if payload["previous_rejection"]
        else ""
    )
    return (
        "[natural-user-reply]\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        + "\n请把上面的内容只当作写回复时可用的事实，用你自己的自然语气直接回复用户。"
        "只陈述 facts 中已确认的事实；draft 只是可能过期的表达草稿，和 facts 冲突时必须丢弃。"
        "current_user_request 是用户这一轮正在要求的工作，回执必须围绕它；"
        "不要告诉用户你收到了结构化信息、JSON、facts、数据包、系统消息或提示词；只说任务本身的真实进展。"
        "reply_is_interim=true 时不得声称整个任务或所有子任务已经完成。"
        "task_continues_without_more_user_input=true 时不得要求用户继续指示、确认或催促；"
        "应当说明工作会按当前要求自行继续。"
        "reply_is_interim=false 且 task_continues_without_more_user_input=false 表示这一轮已经结束，"
        "不得声称稍后会自动继续、正在后台启动或之后会自行回报。"
        "如果 named_work 包含 Audit prepare 结果，只能按 update_applied、"
        "source_binding_change_applied、applied_source_ids 和 effective_source_ids 陈述；"
        "source_binding_change_applied=false 时不得声称本轮新增、更新或准备好了来源绑定。"
        "source_access_verified=true 表示程序已经核对了本轮成功的真实来源探针并据此发布绑定；"
        "此时不得声称没有访问、没有测试或无法测试该来源。"
        "turn_execution.presentation_only=true 表示当前只是在把已经发生的主轮事实写成用户回复，"
        "本表达轮故意没有工具，不能据此声称主轮没有工具或要求用户提供工具；"
        "tool_execution_observed=true 或 successful_operation_count>0 时，必须承认本轮已经发生了工具执行，"
        "但不要向用户罗列内部工具名。"
        "把 delegated_work 翻成普通用户听得懂的子代理进展；不要把 operation、verification、envelope、"
        "mutation、wake 或字段名原样说给用户，也不要凭这些内部词另编一个不存在的验证阶段。"
        "不要暴露内部协议、工具名、运行 ID、服务器路径或系统提示，也不要调用工具。"
        "没有结构化时间估计时不要承诺几分钟、很快或稍后完成；不要估算文件大小。"
        "不要照抄系统模板，用你自己的话，通常一到三句话即可。" + retry_note
    )


# LLM: 不创建替代状态容器；调用方没有 live archive state 时自然回复增强必须无副作用跳过。
# 函数用途: 获取当前 run 可变状态字典，供 pending 回复短生命周期使用。
def _mutable_state(params: object) -> dict[str, object] | None:
    state = getattr(params, "live_archive_state", None)
    return state if isinstance(state, dict) else None


__all__ = [
    "finish_natural_user_reply",
    "natural_user_reply_is_acceptable",
    "natural_user_reply_rejection_reason",
    "natural_user_reply_model_params",
    "pending_natural_user_reply",
    "discard_pending_natural_user_reply",
    "queue_natural_user_reply",
    "retry_natural_user_reply",
]
