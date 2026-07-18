from __future__ import annotations

from ...backends import ModelResponse, is_provider_context_window_error
from ...memory_archive import estimate_tokens
from ..runtime.context_compactor import runtime_compact_policy

# LLM: 本模块是模型调用前的 context-pressure 判定入口；阈值必须只来自 runtime_compact_policy，不能再加隐藏百分比或未来输出预留。
# 模块用途: 在当前输入达到配置的 compact 阈值或工具上下文溢出时，返回结构化压缩信号并阻止继续堆入大输出。


# LLM: preflight 只按当前 prompt/token 事实判断；task_local 子代理仍由自己的上下文链负责压缩。
# 函数用途: 每次调用模型前检查上下文是否达到配置阈值，达到后要求先压缩再继续原任务。
def preflight_context_pressure_response(request: object) -> ModelResponse | None:
    if _uses_task_local_compact(request):
        return None
    policy = runtime_compact_policy(
        getattr(request, "agent", None), save=_request_save_enabled(request)
    )
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
    return context_pressure_response(
        request,
        source="preflight",
        prompt_tokens=prompt_tokens,
        detail=f"prompt_tokens={prompt_tokens} context_window={window} compact_threshold={threshold}",
    )


# LLM: save 的请求级显式值优先于 Agent 默认值，保持与 runtime_compact_policy 的保存语义一致。
# 函数用途: 判断当前请求是否允许保存并应用持久会话 compact。
def _request_save_enabled(request: object) -> bool:
    params = getattr(request, "params", None)
    save = getattr(params, "save", None)
    if save is not None:
        return bool(save)
    config = getattr(getattr(request, "agent", None), "config", None)
    return bool(getattr(config, "auto_save_memory", True))


# LLM: 只认结构化 context_scope，不能从 prompt 或任务文字猜测上下文类型。
# 函数用途: 判断当前调用是否属于拥有独立 session history 的 task_local 子代理。
def _uses_task_local_compact(request: object) -> bool:
    params = getattr(request, "params", None)
    return str(getattr(params, "context_scope", "") or "") == "task_local"


# LLM: 该安全点与 preflight 共用同一 trigger_tokens；禁止为工具轮增加第二个 digest/ceiling 状态机。
# 函数用途: 工具轮准备继续读取或执行大输出前，检查是否应先压缩，避免再把内容塞进已达阈值的上下文。
def should_compact_before_more_tool_output(
    agent: object, params: object, current_prompt: str
) -> bool:
    if not _params_save_enabled(agent, params):
        return False
    if not _has_previous_tool_context(params):
        return False
    policy = runtime_compact_policy(agent, save=True)
    threshold = int(policy.trigger_tokens or 0)
    if threshold <= 0:
        return False
    return estimate_tokens(str(current_prompt or "")) >= threshold


# LLM: 请求参数优先，Agent 配置兜底；这个顺序要与模型 preflight 保持一致。
# 函数用途: 取得工具轮当前是否允许保存并应用 compact。
def _params_save_enabled(agent: object, params: object) -> bool:
    save = getattr(params, "save", None)
    if save is not None:
        return bool(save)
    return bool(getattr(getattr(agent, "config", None), "auto_save_memory", True))


# LLM: 仅在已经有真实工具结果时才延后更多内容工具，避免空工具轮无意义触发。
# 函数用途: 检查当前模型上下文里是否已经积累过工具输出。
def _has_previous_tool_context(params: object) -> bool:
    return any(_is_tool_result_context(item) for item in getattr(params, "tool_context", []) or [])


# LLM: 只识别运行时生成的结构化工具记录前缀，不解析普通自然语言内容。
# 函数用途: 判断一段上下文是不是底座记录的工具调用或工具输出。
def _is_tool_result_context(value: object) -> bool:
    text = str(value or "").lstrip()
    return text.startswith("[tool-record") or "[tool-output-record" in text


# LLM: 该函数以 pop 单次消费 overflow 事件，避免同一溢出在恢复轮重复触发。
# 函数用途: 读取并清除工具上下文裁剪器登记的窗口溢出事实。
def _tool_context_window_overflow(request: object) -> dict[str, object]:
    state = getattr(getattr(request, "params", None), "live_archive_state", None)
    if not isinstance(state, dict):
        return {}
    value = state.pop("tool_context_window_overflow", {})
    return value if isinstance(value, dict) else {}


# LLM: 返回 typed runtime_status=context_overflow 供同一 thread 的 compact/resume 链消费；不能改成用户自然语言控制信号。
# 函数用途: 构造统一的“先压缩再继续”模型响应，并保留触发来源和 token 证据。
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


# LLM: provider 错误分类统一委托 backends，不在这里按错误文本另造判断。
# 函数用途: 判断异常是否属于模型供应商报告的上下文窗口溢出。
def is_context_window_error(exc: BaseException) -> bool:
    return is_provider_context_window_error(exc)


# LLM: 已知 provider/input 计数优先，估算仅作缺失时兜底；返回值不得为负。
# 函数用途: 为结构化 compact 信号提供本次输入 token 数。
def _input_tokens(request: object, prompt_tokens: int) -> int:
    if int(prompt_tokens) > 0:
        return int(prompt_tokens)
    return estimate_tokens(str(getattr(request, "prompt", "") or ""))


__all__ = [
    "context_pressure_response",
    "is_context_window_error",
    "preflight_context_pressure_response",
    "should_compact_before_more_tool_output",
]
