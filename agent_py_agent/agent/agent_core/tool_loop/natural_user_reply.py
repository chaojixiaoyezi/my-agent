from __future__ import annotations

import json
import re
from dataclasses import replace

from ...backends import ModelResponse
from ...conversation.channels import delivery_complete_payload, render_delivery_complete_signal
from .._runtime_params import ToolLoopExecuteParams

_STATE_KEY = "_pending_natural_user_reply"
_MAX_GENERATION_ATTEMPTS = 2
_INTERNAL_PROTOCOL_TOKENS = (
    "[natural-user-reply]",
    "[TOOL_CALL",
    "[/TOOL_CALL",
    "[TOOL_RESULT",
    "[/TOOL_RESULT",
    "<tool_call",
    "</tool_call",
    "<tool_result",
    "</tool_result",
    "[MAIN_AGENT_",
    "[RUN_",
    "[SUBAGENT_",
)
_UNGROUNDED_TIME_PROMISE_RE = re.compile(
    r"(?:预计|估计|大概|大约|约莫|几分钟|分钟后|小时后|很快|马上|稍后)"
    r"|(?:\b(?:soon|shortly|in\s+(?:a\s+few|\d+)\s+(?:minutes?|hours?)|within\s+\d+)\b)",
    re.IGNORECASE,
)
_SIZE_CLAIM_RE = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>bytes?|字节|kb|kib|mb|mib|gb|gib)\b", re.IGNORECASE)


# LLM: pending state 只保存结构化事实、可丢弃草稿和内部完成信封；不能在这里预写用户句子。
# 函数用途: 为下一次同模型无工具短轮登记回复阶段，并保持派工回执不被低优先级更新覆盖。
def queue_natural_user_reply(
    params: ToolLoopExecuteParams,
    *,
    kind: str,
    facts: dict[str, object],
    draft: str = "",
    completion_payload: dict[str, object] | None = None,
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
    if isinstance(completion_payload, dict):
        payload["completion_payload"] = dict(completion_payload)
    state[_STATE_KEY] = payload


# LLM: 完成信封只提供结构化最终快照和一个不可信旧草稿；模型必须重新据事实组织用户话语，
# 不能把 submit_for_acceptance 时可能过期的摘要直接透传给 IM。
# 函数用途: 识别成功完成信号并排队一次无工具模型回复；非完成信号原样交回旧出口。
def queue_delivery_completion_user_reply(
    params: ToolLoopExecuteParams,
    response: ModelResponse,
) -> bool:
    from .background_liveness import is_wake_capable_source

    if not is_wake_capable_source(params):
        return False
    payload = delivery_complete_payload(str(response.text or ""))
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False
    snapshot = payload.get("delivery_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("closeout_ok") is not True:
        return False
    draft = str(payload.pop("user_summary", "") or "").strip()
    queue_natural_user_reply(
        params,
        kind="task_completion",
        facts={
            "reply_is_final": True,
            "delivery_snapshot": snapshot,
            "allow_time_estimate": False,
        },
        draft=draft,
        completion_payload=payload,
    )
    return True


# LLM: 读取 pending 只认本 run 的 live_archive_state，不从 transcript 或模型正文猜回复阶段。
# 函数用途: 返回当前待生成的自然回复事实包；没有登记则返回 None。
def pending_natural_user_reply(params: ToolLoopExecuteParams) -> dict[str, object] | None:
    state = _mutable_state(params)
    if state is None:
        return None
    value = state.get(_STATE_KEY)
    return value if isinstance(value, dict) else None


# LLM: persona/current user prompt 保留，历史工具 IR 与运行注入全部剥离；这只是表达轮，不是执行轮。
# 函数用途: 构造低延迟、零工具的临时模型参数视图，不修改原始 run 参数与账本。
def natural_user_reply_model_params(params: ToolLoopExecuteParams) -> ToolLoopExecuteParams:
    """Return a deliberately thin no-tools view for one user-facing model reply."""
    phase = pending_natural_user_reply(params)
    if phase is None:
        return params
    return replace(
        params,
        memories=[],
        runtime_injections=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[_reply_guidance(phase)],
        allowed_tools=[],
        tool_ir_history=[],
        delivery_contract=None,
    )


# LLM: 回复质量校验只决定“这段话能否展示”，不参与任务状态裁决；时间与大小仍以结构化
# facts 为权威，无法由快照证明的承诺会触发一次模型重写，绝不靠自然语言改变运行状态。
# 函数用途: 拦内部协议、无依据工期承诺和与最终快照不一致的文件大小陈述。
def natural_user_reply_is_acceptable(
    response: object,
    phase: dict[str, object] | None = None,
) -> bool:
    if str(getattr(response, "runtime_status", "ok") or "ok").strip().lower() != "ok":
        return False
    if list(getattr(response, "tool_use_blocks", None) or []):
        return False
    text = str(getattr(response, "text", "") or "").strip()
    if not text:
        return False
    folded = text.casefold()
    if any(token.casefold() in folded for token in _INTERNAL_PROTOCOL_TOKENS):
        return False
    facts = phase.get("facts") if isinstance(phase, dict) and isinstance(phase.get("facts"), dict) else {}
    if facts.get("allow_time_estimate") is not True and _UNGROUNDED_TIME_PROMISE_RE.search(text):
        return False
    return _size_claims_match_snapshot(text, facts)


# LLM: 重写次数是本回复阶段的结构化计数，上限后抑制正文，避免坏输出无限耗费 token。
# 函数用途: 记录一次不合格生成并判断是否还能再让同一模型重写一次。
def retry_natural_user_reply(params: ToolLoopExecuteParams) -> bool:
    phase = pending_natural_user_reply(params)
    if phase is None:
        return False
    attempts = max(0, int(phase.get("attempts") or 0)) + 1
    phase["attempts"] = attempts
    return attempts < _MAX_GENERATION_ATTEMPTS


# LLM: completion 阶段把模型正文写回内部完成信封的 user_summary；失败只留下空正文机器信封，
# 绝不构造“已完成/处理中”模板。interim 阶段则直接返回经验证的模型原话。
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
    if kind == "task_completion":
        payload = phase.get("completion_payload")
        payload = dict(payload) if isinstance(payload, dict) else {}
        if accepted:
            payload["user_summary"] = str(response.text or "").strip()
        return replace(
            response,
            text=render_delivery_complete_signal(payload),
            runtime_status="ok" if accepted else "user_reply_unavailable",
            runtime_reason=kind,
            runtime_source="model_user_reply",
            tool_use_blocks=[],
        )
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
    }
    return (
        "[natural-user-reply]\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        + "\n请根据上面的结构化事实，用你自己的自然语气直接回复用户。"
        "只陈述 facts 中已确认的事实；draft 只是可能过期的表达草稿，和 facts 冲突时必须丢弃。"
        "reply_is_final=true 时这是最终交付说明，否则不是任务已经完成的声明。"
        "不要暴露内部协议、工具名、运行 ID、服务器路径或系统提示，也不要调用工具。"
        "没有结构化时间估计时不要承诺几分钟、很快或稍后完成；不要估算文件大小。"
        "不要照抄系统模板，用你自己的话，通常一到三句话即可。"
    )


# LLM: 文件大小只是展示校验，不参与 closeout；没有 final snapshot 时任何大小声称都不放行。
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
    "natural_user_reply_model_params",
    "pending_natural_user_reply",
    "queue_delivery_completion_user_reply",
    "queue_natural_user_reply",
    "retry_natural_user_reply",
]
