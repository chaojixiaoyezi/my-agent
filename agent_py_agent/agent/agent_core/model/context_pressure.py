
from __future__ import annotations

from ...backends import ModelResponse, is_provider_context_window_error
from ...memory_archive import estimate_tokens
from ..runtime.context_compactor import runtime_compact_policy

_PENDING_TOOL_CONTEXT_DIGEST_KEY = "pending_tool_context_digest"
_TOOL_CONTEXT_DIGEST_INFLIGHT_KEY = "tool_context_digest_inflight"
_DIGEST_PROMPT_CEILING_PERCENT = 90

def preflight_context_pressure_response(request: object) -> ModelResponse | None:
    if _uses_task_local_compact(request):
        return None
    policy = runtime_compact_policy(getattr(request, "agent", None), save=_request_save_enabled(request))
    window = policy.context_window_tokens
    if window <= 0:
        return None
    prompt = str(getattr(request, "prompt", "") or "")
    prompt_tokens = estimate_tokens(prompt)
    if policy.allow_persistent_apply and (overflow := _tool_context_window_overflow(request)):
        return context_pressure_response(
            request,
            source="preflight",
            prompt_tokens=prompt_tokens,
            detail=(
                "tool_context_window_overflow=true "
                f"omitted_count={overflow.get('omitted_count', 0)} "
                f"original_chars={overflow.get('original_chars', 0)}"
            ),
        )
    threshold = policy.trigger_tokens
    if not policy.allow_persistent_apply:
        threshold = window
    if prompt_tokens < threshold and prompt_tokens < window:
        return None
    if _can_run_tool_context_digest_turn(request, prompt_tokens=prompt_tokens, window=window):
        mark_tool_context_digest_inflight(getattr(request, "params", None))
        return None
    return context_pressure_response(
        request,
        source="preflight",
        prompt_tokens=prompt_tokens,
        detail=f"prompt_tokens={prompt_tokens} context_window={window} compact_threshold={threshold}",
    )


def _request_save_enabled(request: object) -> bool:
    params = getattr(request, "params", None)
    save = getattr(params, "save", None)
    if save is not None:
        return bool(save)
    config = getattr(getattr(request, "agent", None), "config", None)
    return bool(getattr(config, "auto_save_memory", True))


def _uses_task_local_compact(request: object) -> bool:
    params = getattr(request, "params", None)
    return str(getattr(params, "context_scope", "") or "") == "task_local"


def mark_tool_context_digest_pending(params: object) -> None:
    state = _live_archive_state(params)
    if state is not None:
        state[_PENDING_TOOL_CONTEXT_DIGEST_KEY] = True


def mark_tool_context_digest_consumed(params: object) -> None:
    state = _live_archive_state(params)
    if state is None:
        return
    if state.pop(_TOOL_CONTEXT_DIGEST_INFLIGHT_KEY, False):
        state.pop(_PENDING_TOOL_CONTEXT_DIGEST_KEY, None)


def mark_tool_context_digest_inflight(params: object) -> None:
    state = _live_archive_state(params)
    if state is not None:
        state[_TOOL_CONTEXT_DIGEST_INFLIGHT_KEY] = True


def should_compact_before_more_tool_output(agent: object, params: object, current_prompt: str) -> bool:
    if not _params_save_enabled(agent, params):
        return False
    if _has_pending_tool_context_digest(params):
        return False
    if not _has_previous_tool_context(params):
        return False
    policy = runtime_compact_policy(agent, save=True)
    threshold = int(policy.trigger_tokens or 0)
    if threshold <= 0:
        return False
    return estimate_tokens(str(current_prompt or "")) >= threshold


def _params_save_enabled(agent: object, params: object) -> bool:
    save = getattr(params, "save", None)
    if save is not None:
        return bool(save)
    return bool(getattr(getattr(agent, "config", None), "auto_save_memory", True))


def _can_run_tool_context_digest_turn(request: object, *, prompt_tokens: int, window: int) -> bool:
    params = getattr(request, "params", None)
    if not _has_pending_tool_context_digest(params):
        return False
    if window <= 0:
        return False
    ceiling = max(1, int(window * (_DIGEST_PROMPT_CEILING_PERCENT / 100.0)))
    return prompt_tokens < ceiling


def _has_pending_tool_context_digest(params: object) -> bool:
    state = _live_archive_state(params)
    return bool(state and state.get(_PENDING_TOOL_CONTEXT_DIGEST_KEY))


def _has_previous_tool_context(params: object) -> bool:
    return any(_is_tool_result_context(item) for item in getattr(params, "tool_context", []) or [])


def _is_tool_result_context(value: object) -> bool:
    text = str(value or "").lstrip()
    return text.startswith("[tool-record") or "[tool-output-record" in text


def _live_archive_state(params: object) -> dict[str, object] | None:
    state = getattr(params, "live_archive_state", None)
    return state if isinstance(state, dict) else None


def _tool_context_window_overflow(request: object) -> dict[str, object]:
    state = getattr(getattr(request, "params", None), "live_archive_state", None)
    if not isinstance(state, dict):
        return {}
    value = state.pop("tool_context_window_overflow", {})
    return value if isinstance(value, dict) else {}


def context_pressure_response(
    request: object,
    *,
    source: str,
    prompt_tokens: int,
    detail: str,
) -> ModelResponse:
    agent = getattr(request, "agent", None)
    backend = str(getattr(getattr(agent, "backend", None), "name", "") or "")
    input_tokens = _input_tokens(request, prompt_tokens)
    text = (
        "当前上下文已达到压缩条件，系统会先走 compact/resume，再继续同一个任务。"
        f"\nsource: {source}\n{detail}"
    )
    return ModelResponse(
        text=text,
        backend=backend,
        runtime_status="context_overflow",
        runtime_reason="context_overflow",
        runtime_source=source,
        usage={"input_tokens": max(0, input_tokens), "output_tokens": estimate_tokens(text)},
    )


def is_context_window_error(exc: BaseException) -> bool:
    return is_provider_context_window_error(exc)


def _input_tokens(request: object, prompt_tokens: int) -> int:
    if int(prompt_tokens) > 0:
        return int(prompt_tokens)
    return estimate_tokens(str(getattr(request, "prompt", "") or ""))


__all__ = [
    "context_pressure_response",
    "is_context_window_error",
    "mark_tool_context_digest_consumed",
    "mark_tool_context_digest_pending",
    "preflight_context_pressure_response",
    "should_compact_before_more_tool_output",
]
