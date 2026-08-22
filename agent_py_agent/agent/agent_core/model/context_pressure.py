from __future__ import annotations

from dataclasses import dataclass

from ...backends import ModelResponse, is_provider_context_window_error
from ...memory_archive import estimate_tokens
from ..native_tool_protocol import native_tool_use_active, resolve_native_tools
from ..runtime.context_compactor import runtime_compact_policy

# LLM: 本模块是模型调用前的 context-pressure 判定入口；阈值必须只来自 runtime_compact_policy，不能再加隐藏百分比或未来输出预留。
# 模块用途: 在当前输入达到配置的 compact 阈值或工具上下文溢出时，返回结构化压缩信号并阻止继续堆入大输出。


@dataclass(frozen=True)
class ModelVisibleContextBudget:
    """One runtime-owned snapshot of the provider-visible context budget."""

    context_window_tokens: int
    compact_trigger_tokens: int
    current_tokens: int
    remaining_to_compact_tokens: int


# LLM: This snapshot is the public, read-only projection of the exact preflight total plus
# estimated provider-visible component shares; component totals must add back to current_tokens.
# 类用途: 保存一次真实模型调用前的上下文总量、窗口、压缩线和各组成部分，供 TUI 实时展示。
@dataclass(frozen=True)
class ModelVisibleContextSnapshot:
    context_window_tokens: int
    compact_trigger_tokens: int
    current_tokens: int
    prompt_tokens: int
    messages_tokens: int
    runtime_guidance_tokens: int
    tool_schema_tokens: int
    protocol: str

    # LLM: Public serialization exposes only bounded numeric facts and the frozen schema id;
    # prompts, messages, tool definitions, and guidance content must never enter the UI event.
    # 函数用途: 把上下文快照转换成可安全发送给 TUI 的结构化字典，不包含任何正文。
    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": "model_visible_context_usage.v1",
            "estimated": True,
            "context_window_tokens": self.context_window_tokens,
            "compact_trigger_tokens": self.compact_trigger_tokens,
            "current_tokens": self.current_tokens,
            "prompt_tokens": self.prompt_tokens,
            "messages_tokens": self.messages_tokens,
            "runtime_guidance_tokens": self.runtime_guidance_tokens,
            "tool_schema_tokens": self.tool_schema_tokens,
            "protocol": self.protocol,
        }


# LLM: This is the only constructor for live context-display facts. It reuses the same
# runtime_compact_policy and provider-visible estimator as preflight instead of adding a UI budget.
# The configured trigger remains stable even when one presentation/no-save call cannot apply it;
# call-local persistence permission belongs to enforcement, not the public conversation policy.
# 函数用途: 计算一次即将发给模型的上下文构成；状态条始终显示统一的自动压缩点，不受单次回合是否允许落盘影响。
def model_visible_context_snapshot(
    agent: object,
    params: object | None,
    prompt: str,
) -> ModelVisibleContextSnapshot:
    protocol, current, components = _model_visible_context_components(
        agent,
        params,
        prompt,
    )
    policy = runtime_compact_policy(
        agent,
        save=_params_save_enabled(agent, params),
        context_scope=str(getattr(params, "context_scope", "") or "default"),
    )
    window = max(0, int(policy.context_window_tokens or 0))
    trigger = max(0, int(policy.trigger_tokens or 0))
    if trigger <= 0:
        trigger = window
    return ModelVisibleContextSnapshot(
        context_window_tokens=window,
        compact_trigger_tokens=trigger,
        current_tokens=current,
        prompt_tokens=components["prompt_tokens"],
        messages_tokens=components["messages_tokens"],
        runtime_guidance_tokens=components["runtime_guidance_tokens"],
        tool_schema_tokens=components["tool_schema_tokens"],
        protocol=protocol,
    )


def model_visible_context_budget(
    agent: object,
    params: object | None = None,
    prompt: str | None = None,
) -> ModelVisibleContextBudget:
    """Return the same window/current-token facts used by preflight compact.

    Tools may use this snapshot as a resource boundary, but may not infer task
    meaning or start a second compaction policy from it.
    """
    resolved_params = params if params is not None else getattr(agent, "_current_run_params", None)
    resolved_prompt = (
        str(prompt)
        if prompt is not None
        else str(getattr(agent, "_current_user_prompt", "") or "")
    )
    snapshot = model_visible_context_snapshot(agent, resolved_params, resolved_prompt)
    window = snapshot.context_window_tokens
    trigger = snapshot.compact_trigger_tokens
    current = snapshot.current_tokens
    return ModelVisibleContextBudget(
        context_window_tokens=window,
        compact_trigger_tokens=trigger,
        current_tokens=current,
        remaining_to_compact_tokens=max(0, trigger - current),
    )


def safe_inline_tool_result_tokens(
    agent: object,
    params: object | None = None,
    prompt: str | None = None,
) -> int:
    """Return the exact remaining headroom before the unified compact trigger.

    Large durable tool results keep their canonical content behind references;
    callers may shrink a delivered view at complete-record boundaries.  A
    second fixed percentage or token cap here would silently override the
    configured compact policy and make a 90% trigger behave like 15%.
    """
    budget = model_visible_context_budget(agent, params=params, prompt=prompt)
    if budget.context_window_tokens <= 0 or budget.remaining_to_compact_tokens <= 0:
        return 0
    return budget.remaining_to_compact_tokens


# LLM: preflight 只按当前 prompt/token 事实判断；主代理和 task_local 子代理共用同一条 compact 链。
# 函数用途: 每次调用模型前检查上下文是否达到配置阈值，达到后要求先压缩再继续原任务。
def preflight_context_pressure_response(request: object) -> ModelResponse | None:
    params = getattr(request, "params", None)
    policy = runtime_compact_policy(
        getattr(request, "agent", None),
        save=_request_save_enabled(request),
        context_scope=str(getattr(params, "context_scope", "") or "default"),
    )
    window = policy.context_window_tokens
    if window <= 0:
        return None
    prompt = str(getattr(request, "prompt", "") or "")
    prompt_tokens = model_visible_context_tokens(
        getattr(request, "agent", None),
        getattr(request, "params", None),
        prompt,
    )
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
        detail=(
            f"model_visible_tokens={prompt_tokens} "
            f"context_window={window} compact_threshold={threshold}"
        ),
    )


# LLM: This is the one preflight accounting path for the exact provider-visible input. Native
# tool schemas count even before the first tool call; otherwise an empty IR history makes a large
# fixed schema disappear from the compact decision.
# 函数用途: 统一统计文字 prompt、原生工具 schema、原生工具消息和待转发运行时引导。
def model_visible_context_tokens(agent: object, params: object, prompt: str) -> int:
    return _model_visible_context_components(agent, params, prompt)[1]


# LLM: Component estimates are derived only from the four actual provider-visible values. They
# are proportionally normalized so their displayed sum is exactly the canonical total estimate.
# 函数用途: 分别估算 prompt、历史消息、运行指引和工具定义的占比，同时保持总数与原 compact 口径一致。
def _model_visible_context_components(
    agent: object,
    params: object | None,
    prompt: str,
) -> tuple[str, int, dict[str, int]]:
    if not native_tool_use_active(params):
        current = estimate_tokens(str(prompt or ""))
        return (
            "text",
            current,
            {
                "prompt_tokens": current,
                "messages_tokens": 0,
                "runtime_guidance_tokens": 0,
                "tool_schema_tokens": 0,
            },
        )

    from ...backends.message_adapter import AnthropicMessageAdapter
    from ..tool_ir_guidance import unforwarded_runtime_guidance

    history = list(getattr(params, "tool_ir_history", None) or [])
    messages = (
        AnthropicMessageAdapter().to_provider_messages(history)
        if history
        else []
    )
    state = getattr(params, "live_archive_state", None)
    already_forwarded = (
        set(state.get("_forwarded_runtime_guidance", set()))
        if isinstance(state, dict)
        else set()
    )
    guidance = unforwarded_runtime_guidance(
        getattr(params, "tool_context", None),
        already_forwarded,
    )
    tools = resolve_native_tools(agent, params) or []
    payload = {
        "initial_user_prompt": str(prompt or ""),
        "messages": messages,
        "pending_runtime_guidance": guidance,
        "tools": tools,
    }
    current = estimate_tokens(payload)
    components = _normalize_context_component_tokens(
        current,
        (
            ("prompt_tokens", str(prompt or "")),
            ("messages_tokens", messages),
            ("runtime_guidance_tokens", guidance),
            ("tool_schema_tokens", tools),
        ),
    )
    return "native", current, components


# LLM: The estimator has per-object overhead and rounding, so independently estimated categories
# cannot be added directly. Integer proportional allocation preserves ordering and the exact total.
# 函数用途: 把各部分的原始估算按比例分摊到总 token，避免分类相加与状态条总数对不上。
def _normalize_context_component_tokens(
    total_tokens: int,
    values: tuple[tuple[str, object], ...],
) -> dict[str, int]:
    total = max(0, int(total_tokens or 0))
    weights = [
        estimate_tokens(value) if value not in (None, "", [], (), {}) else 0
        for _, value in values
    ]
    weight_total = sum(weights)
    if weight_total <= 0:
        return {
            key: total if index == 0 else 0
            for index, (key, _) in enumerate(values)
        }
    allocated = [weight * total // weight_total for weight in weights]
    remainder = total - sum(allocated)
    order = sorted(
        range(len(values)),
        key=lambda index: (-(weights[index] * total % weight_total), index),
    )
    for index in order[:remainder]:
        allocated[index] += 1
    return {
        key: allocated[index]
        for index, (key, _) in enumerate(values)
    }


# LLM: save 的请求级显式值优先于 Agent 默认值，保持与 runtime_compact_policy 的保存语义一致。
# 函数用途: 判断当前请求是否允许保存并应用持久会话 compact。
def _request_save_enabled(request: object) -> bool:
    params = getattr(request, "params", None)
    save = getattr(params, "save", None)
    if save is not None:
        return bool(save)
    config = getattr(getattr(request, "agent", None), "config", None)
    return bool(getattr(config, "auto_save_memory", True))


# LLM: 该安全点与 preflight 共用同一 trigger_tokens；禁止为工具轮增加第二个 digest/ceiling 状态机。
# 函数用途: 工具轮准备继续读取或执行大输出前，检查是否应先压缩，避免再把内容塞进已达阈值的上下文。
def should_compact_before_more_tool_output(
    agent: object, params: object, current_prompt: str
) -> bool:
    if not _params_save_enabled(agent, params):
        return False
    if not _has_previous_tool_context(params):
        return False
    policy = runtime_compact_policy(
        agent,
        save=True,
        context_scope=str(getattr(params, "context_scope", "") or "default"),
    )
    threshold = int(policy.trigger_tokens or 0)
    if threshold <= 0:
        return False
    # EXEC-16: native 协议下工具结果在 IR messages 里、不在 prompt 文本里——
    # estimate_tokens(prompt) 恒为静态前缀(≈9K), 永远到不了阈值 → compact 永不触发
    # (ma 双线 36 轮仍 archive_events=0 实锤)。改用与 preflight 同口径的
    # model_visible_context_tokens: native 下含 IR messages/tools/guidance。
    return model_visible_context_tokens(agent, params, str(current_prompt or "")) >= threshold


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
    # EXEC-16: native 协议下工具结果在 IR 历史里、tool_context 文本链只放
    # "[assistant-tool-round-N]" 轮标记(不含 tool-record 前缀), 文本判定恒 False →
    # compact 被误挡。native 下以 IR 历史是否有工具结果为准。
    if native_tool_use_active(params):
        from ...tooling.runtime_contracts import ToolResult

        history = list(getattr(params, "tool_ir_history", None) or [])
        return any(isinstance(item, ToolResult) for item in history)
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
    # This is an internal lifecycle event, not an assistant answer.  Prefix it
    # with the existing channel-level internal signal envelope so any caller
    # that does not own inline continuation still fails closed at delivery.
    text = f"[RUN_CONTEXT_PRESSURE]\nsource: {source}\n{detail}"
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
    "ModelVisibleContextBudget",
    "ModelVisibleContextSnapshot",
    "context_pressure_response",
    "is_context_window_error",
    "model_visible_context_budget",
    "model_visible_context_snapshot",
    "model_visible_context_tokens",
    "preflight_context_pressure_response",
    "safe_inline_tool_result_tokens",
    "should_compact_before_more_tool_output",
]
