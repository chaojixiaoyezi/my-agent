
from __future__ import annotations

from ...backends import ModelResponse
from ...memory_archive import estimate_tokens
from ..runtime.context_compactor import runtime_compact_policy

_CONTEXT_ERROR_MARKERS = (
    "context length",
    "context_length",
    "context window",
    "context_window",
    "maximum context",
    "max context",
    "input too long",
    "prompt too long",
    "too many tokens",
    "token limit",
)


def preflight_context_pressure_response(request: object) -> ModelResponse | None:
    if _uses_task_local_compact(request):
        return None
    policy = runtime_compact_policy(getattr(request, "agent", None), save=True)
    window = policy.context_window_tokens
    if window <= 0:
        return None
    prompt = str(getattr(request, "prompt", "") or "")
    prompt_tokens = estimate_tokens(prompt)
    if overflow := _tool_context_window_overflow(request):
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
    if prompt_tokens < threshold and prompt_tokens < window:
        return None
    return context_pressure_response(
        request,
        source="preflight",
        prompt_tokens=prompt_tokens,
        detail=f"prompt_tokens={prompt_tokens} context_window={window} compact_threshold={threshold}",
    )


def _uses_task_local_compact(request: object) -> bool:
    params = getattr(request, "params", None)
    return str(getattr(params, "context_scope", "") or "") == "task_local"


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
    text = str(exc).lower()
    return any(marker in text for marker in _CONTEXT_ERROR_MARKERS)


def _input_tokens(request: object, prompt_tokens: int) -> int:
    if int(prompt_tokens) > 0:
        return int(prompt_tokens)
    return estimate_tokens(str(getattr(request, "prompt", "") or ""))


__all__ = [
    "context_pressure_response",
    "is_context_window_error",
    "preflight_context_pressure_response",
]
