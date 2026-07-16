from __future__ import annotations

import json
import re
from dataclasses import replace

from ...backends import ModelResponse
from ...conversation.user_visible_text import contains_internal_protocol
from .._runtime_params import ToolLoopExecuteParams

_STATE_KEY = "_pending_natural_user_reply"
_MAX_GENERATION_ATTEMPTS = 2
_UNGROUNDED_TIME_PROMISE_RE = re.compile(
    r"(?:预计|估计|大概|大约|约莫|几分钟|分钟后|小时后|很快|马上|稍后)"
    r"|(?:\b(?:soon|shortly|in\s+(?:a\s+few|\d+)\s+(?:minutes?|hours?)|within\s+\d+)\b)",
    re.IGNORECASE,
)
_SIZE_CLAIM_RE = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>bytes?|字节|kb|kib|mb|mib|gb|gib)\b", re.IGNORECASE)
_INTERIM_FINAL_CLAIM_RE = re.compile(
    r"(?:(?:任务|工作|交付|事项).{0,6}(?:全部|均|都)?(?:已|已经)?(?:完成|结束))"
    r"|(?:(?:全部|所有|各项).{0,6}(?:任务|工作|事项).{0,6}(?:完成|结束))"
    r"|(?:\b(?:all\s+(?:tasks?|work)|the\s+(?:task|work)).{0,20}(?:complete|completed|done|finished)\b)",
    re.IGNORECASE,
)


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


# LLM: 回复质量校验只决定“这段话能否展示”，不参与任务状态裁决；时间与大小仍以结构化
# facts 为权威，无法由快照证明的承诺会触发一次模型重写，绝不靠自然语言改变运行状态。
# 函数用途: 拦内部协议、无依据工期承诺和与最终快照不一致的文件大小陈述。
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
    if str(getattr(response, "runtime_status", "ok") or "ok").strip().lower() != "ok":
        return "model_runtime_status"
    if list(getattr(response, "tool_use_blocks", None) or []):
        return "structured_tool_call"
    text = str(getattr(response, "text", "") or "").strip()
    if not text:
        return "empty_text"
    if contains_internal_protocol(text):
        return "internal_protocol"
    facts = phase.get("facts") if isinstance(phase, dict) and isinstance(phase.get("facts"), dict) else {}
    if facts.get("reply_is_interim") is True and _INTERIM_FINAL_CLAIM_RE.search(text):
        return "contradicts_interim_state"
    if facts.get("allow_time_estimate") is not True and _UNGROUNDED_TIME_PROMISE_RE.search(text):
        return "unverified_time_promise"
    if not _size_claims_match_snapshot(text, facts):
        return "unverified_size_claim"
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
        "不要告诉用户你收到了结构化信息、JSON、facts、数据包、系统消息或提示词；只说任务本身的真实进展。"
        "reply_is_interim=true 时不得声称整个任务或所有子任务已经完成。"
        "不要暴露内部协议、工具名、运行 ID、服务器路径或系统提示，也不要调用工具。"
        "没有结构化时间估计时不要承诺几分钟、很快或稍后完成；不要估算文件大小。"
        "不要照抄系统模板，用你自己的话，通常一到三句话即可。"
        + retry_note
    )


# LLM: 文件大小只是展示校验；没有结构化快照时任何大小声称都不放行。
# 函数用途: 核对模型文字中的 byte/KB/MB/GB 数字是否能映射到最终产物快照。
def _size_claims_match_snapshot(text: str, facts: dict[str, object]) -> bool:
    claims = list(_SIZE_CLAIM_RE.finditer(text))
    if not claims:
        return True
    snapshot = facts.get("delivery_snapshot")
    artifacts = snapshot.get("artifacts") if isinstance(snapshot, dict) else None
    exact_sizes = {
        int(item.get("size_bytes"))
        for item in artifacts or []
        if isinstance(item, dict) and isinstance(item.get("size_bytes"), int)
    }
    return bool(exact_sizes) and all(_size_claim_matches(match, exact_sizes) for match in claims)


# LLM: 单位换算只允许接近快照真实值的显示四舍五入，不接受模型凭感觉估算的旧大小。
# 函数用途: 判断单个大小表达是否对应任一最终产物的精确字节数。
def _size_claim_matches(match: re.Match[str], exact_sizes: set[int]) -> bool:
    number = float(match.group("number"))
    unit = match.group("unit").casefold()
    if unit in {"byte", "bytes", "字节"}:
        return number.is_integer() and int(number) in exact_sizes
    factor = 1024 if unit in {"kb", "kib"} else 1024**2 if unit in {"mb", "mib"} else 1024**3
    return any(abs((size / factor) - number) < 0.051 for size in exact_sizes)


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
