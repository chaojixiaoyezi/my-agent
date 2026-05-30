# LLM: Context pressure helpers decide when to compact before or after provider calls.
# 模块用途: 将上下文窗口预检、provider 撞墙识别和 compact 触发响应统一在一处。

from __future__ import annotations

from ..backends import ModelResponse
from ..memory_archive import estimate_tokens
from .runtime_context_compactor import runtime_compact_policy

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


# LLM: preflight_context_pressure_response avoids sending obviously over-budget prompts.
# 函数用途: 模型调用前按当前 prompt 和上下文窗口判断是否应先 compact；task-local 子代理 compact 由本地链路处理。
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


# LLM: _uses_task_local_compact prevents global compact from hijacking subagent local compaction.
# 函数用途: 判断当前调用是否属于子代理 task-local compact 链路；是则跳过主代理级预检。
def _uses_task_local_compact(request: object) -> bool:
    params = getattr(request, "params", None)
    return str(getattr(params, "context_scope", "") or "") == "task_local"


# LLM: _tool_context_window_overflow lifts hidden live transcript trimming into the regular compact path.
# 函数用途: 读取工具上下文窗口裁剪信号，让长任务不靠第二套隐形压缩长期跑下去。
def _tool_context_window_overflow(request: object) -> dict[str, object]:
    state = getattr(getattr(request, "params", None), "live_archive_state", None)
    if not isinstance(state, dict):
        return {}
    value = state.pop("tool_context_window_overflow", {})
    return value if isinstance(value, dict) else {}


# LLM: context_pressure_response creates a model-like result that finalization can compact from.
# 函数用途: 把预检超限或 provider 上下文错误转换成统一 runtime_status，而不是直接抛出中断。
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


# LLM: is_context_window_error recognizes provider-side context overflow messages.
# 函数用途: 捕获 input too long / context exceeded 等错误，让它们走 compact/resume 兜底。
def is_context_window_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CONTEXT_ERROR_MARKERS)


# LLM: _input_tokens chooses a caller-supplied estimate or recomputes from prompt text.
# 函数用途: 给 provider 错误兜底生成 usage.input_tokens，避免 0 污染 compact 预算。
def _input_tokens(request: object, prompt_tokens: int) -> int:
    if int(prompt_tokens) > 0:
        return int(prompt_tokens)
    return estimate_tokens(str(getattr(request, "prompt", "") or ""))


__all__ = [
    "context_pressure_response",
    "is_context_window_error",
    "preflight_context_pressure_response",
]
