from __future__ import annotations

import json
from dataclasses import replace

from ...backends import ModelResponse
from ...conversation.user_visible_text import contains_internal_protocol
from .._runtime_params import ToolLoopExecuteParams

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
#   并再次吐出工具协议。结构化回复事实成为这个零工具短轮唯一的 User Task。
# 函数用途: 构造低延迟、零工具、不重做原任务的临时模型参数视图。
def natural_user_reply_model_params(params: ToolLoopExecuteParams) -> ToolLoopExecuteParams:
    """Return a deliberately thin no-tools view for one user-facing model reply."""
    phase = pending_natural_user_reply(params)
    if phase is None:
        return params
    guidance = _reply_guidance(phase)
    return replace(
        params,
        user_prompt=guidance,
        memories=[],
        # isolated 会跳过项目默认的工具/执行规约，仍保留 owner 人格入口；
        # guidance 放在最后的 User Task，所以 text/native 后端都一定看得到。
        runtime_injections=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        allowed_tools=[],
        tool_ir_history=[],
        delivery_contract=None,
        context_scope="isolated",
        consume_pending_turn_input=False,
    )


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
    if contains_internal_protocol(text):
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
) -> ModelResponse:
    state = _mutable_state(params)
    phase = state.pop(_STATE_KEY, None) if state is not None else None
    phase = phase if isinstance(phase, dict) else {}
    kind = str(phase.get("kind") or "background_update")
    if not accepted:
        return replace(
            response,
            text="",
            runtime_status="user_reply_unavailable",
            runtime_reason=kind,
            runtime_source="model_user_reply",
            tool_use_blocks=[],
        )
    return replace(
        response,
        text=str(response.text or "").strip(),
        runtime_status="ok",
        runtime_reason=kind,
        runtime_source="model_user_reply",
        tool_use_blocks=[],
    )


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
        "existing_task_selected_this_turn=true 表示旧任务已续接，但旧进度没有在本轮刷新，不能把旧步骤说成这一轮的进展。"
        "prior_task_context_available=true 表示 current_task_goal、工作区名称和已提交的任务引导属于当前这个持久任务；"
        "应据此明确承认是在续接原任务，不得声称没有之前的上下文，也不得要求用户重发已经列出的目标、路径或补充要求。"
        "recent_committed_task_guidance 只是当前任务最近已确认的要求，不要逐条复述，按需自然概括即可。"
        "task_workspace_selected_this_turn=true 表示原任务和原工作区已经精确选定，不要再向用户索要项目路径或 README。"
        "runtime_access_confirmed=true 表示本轮运行时已经成功访问过工作区；表达轮本身不带工具只是为了写回复，"
        "绝不代表执行环境没有工具，因此不要声称没有工具、不能操作文件或需要用户重新提供环境。"
        "不要告诉用户你收到了结构化信息、JSON、facts、数据包、系统消息或提示词；只说任务本身的真实进展。"
        "reply_is_interim=true 时不得声称整个任务或所有子任务已经完成。"
        "不要暴露内部协议、工具名、运行 ID、服务器路径或系统提示，也不要调用工具。"
        "没有结构化时间估计时不要承诺几分钟、很快或稍后完成；不要估算文件大小。"
        "不要照抄系统模板，用你自己的话，通常一到三句话即可。"
        + retry_note
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
