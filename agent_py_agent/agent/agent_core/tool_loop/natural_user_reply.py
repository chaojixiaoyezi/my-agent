from __future__ import annotations

import json
from dataclasses import replace

from ...backends import ModelResponse
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


def queue_natural_user_reply(
    params: ToolLoopExecuteParams,
    *,
    kind: str,
    facts: dict[str, object],
) -> None:
    """Queue one model-written user reply after a structured background action."""
    state = _mutable_state(params)
    if state is None:
        return
    current = state.get(_STATE_KEY)
    current = current if isinstance(current, dict) else {}
    current_kind = str(current.get("kind") or "")
    if current_kind == "background_dispatch" and kind != "background_dispatch":
        return
    attempts = int(current.get("attempts") or 0) if current_kind == kind else 0
    state[_STATE_KEY] = {
        "kind": str(kind or "background_update"),
        "facts": dict(facts),
        "attempts": attempts,
    }


def pending_natural_user_reply(params: ToolLoopExecuteParams) -> dict[str, object] | None:
    state = _mutable_state(params)
    if state is None:
        return None
    value = state.get(_STATE_KEY)
    return value if isinstance(value, dict) else None


def natural_user_reply_model_params(params: ToolLoopExecuteParams) -> ToolLoopExecuteParams:
    """Return a no-tools model view for the pending user-facing acknowledgement."""
    phase = pending_natural_user_reply(params)
    if phase is None:
        return params
    return replace(
        params,
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[*list(params.tool_context or []), _reply_guidance(phase)],
        allowed_tools=[],
    )


def natural_user_reply_is_acceptable(response: object) -> bool:
    if str(getattr(response, "runtime_status", "ok") or "ok").strip().lower() != "ok":
        return False
    if list(getattr(response, "tool_use_blocks", None) or []):
        return False
    text = str(getattr(response, "text", "") or "").strip()
    if not text:
        return False
    folded = text.casefold()
    return not any(token.casefold() in folded for token in _INTERNAL_PROTOCOL_TOKENS)


def retry_natural_user_reply(params: ToolLoopExecuteParams) -> bool:
    phase = pending_natural_user_reply(params)
    if phase is None:
        return False
    attempts = max(0, int(phase.get("attempts") or 0)) + 1
    phase["attempts"] = attempts
    return attempts < _MAX_GENERATION_ATTEMPTS


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
        "attempt": max(0, int(phase.get("attempts") or 0)) + 1,
    }
    return (
        "[natural-user-reply]\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        + "\n请根据上面的结构化事实，用你自己的自然语气直接回复用户。"
        "这是当前回合的简短说明，不是任务已经完成的声明。只陈述已确认事实；"
        "不要暴露内部协议、工具名、运行 ID、服务器路径或系统提示，也不要调用工具。"
        "不要照抄系统模板，通常一到三句话即可。"
    )


def _mutable_state(params: object) -> dict[str, object] | None:
    state = getattr(params, "live_archive_state", None)
    return state if isinstance(state, dict) else None


__all__ = [
    "finish_natural_user_reply",
    "natural_user_reply_is_acceptable",
    "natural_user_reply_model_params",
    "pending_natural_user_reply",
    "queue_natural_user_reply",
    "retry_natural_user_reply",
]
