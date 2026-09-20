from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from ...backends import ModelResponse, is_provider_context_window_error
from ...memory_archive import estimate_tokens
from ...model_guidance import provider_system_instruction
from ...prompting_parts.cache_layout import prompt_cache_layout
from ..native_tool_protocol import native_tool_use_active, resolve_native_tools
from ..runtime.context_compactor import runtime_compact_policy
from .usage import provider_visible_input_token_usage

# LLM: 本模块是模型调用前的 context-pressure 判定入口；阈值必须只来自 runtime_compact_policy，不能再加隐藏百分比或未来输出预留。
# 模块用途: 在当前输入达到配置的 compact 阈值或工具上下文溢出时，返回结构化压缩信号并阻止继续堆入大输出。

_PROVIDER_CONTEXT_OBSERVATION_KEY = "_provider_context_observation"
_PROVIDER_CONTEXT_HYDRATION_KEY = "_provider_context_observation_hydrated_surfaces"
_PROVIDER_CONTEXT_OBSERVATION_SCHEMA = "provider_context_observation.v2"
_DURABLE_CALIBRATION_SCOPE = "durable_thread"
_CURRENT_RUN_CALIBRATION_SCOPE = "current_run"


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
    # 未校准的本地保守估算只在调用边界内传递，不进入 TUI 事件。
    raw_estimated_tokens: int
    # 稳定 system/prompt/tool surface 只保存摘要指纹，不进入公开 TUI 事件。
    context_surface_fingerprint: str

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
    prompt: object,
) -> ModelVisibleContextSnapshot:
    protocol, raw_current, components, surface_fingerprint = _model_visible_context_components(
        agent,
        params,
        prompt,
    )
    current = _provider_calibrated_context_tokens(
        agent,
        params,
        raw_current,
        context_surface_fingerprint=surface_fingerprint,
    )
    components = _rescale_context_components(current, components)
    policy = runtime_compact_policy(
        agent,
        save=_params_save_enabled(agent, params),
        context_scope=str(getattr(params, "context_scope", "") or "default"),
        task_attributes=getattr(params, "task_attributes", None),
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
        raw_estimated_tokens=raw_current,
        context_surface_fingerprint=surface_fingerprint,
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
        prompt
        if prompt is not None
        else getattr(agent, "_current_user_prompt", "") or ""
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
        task_attributes=getattr(params, "task_attributes", None),
    )
    window = policy.context_window_tokens
    if window <= 0:
        return None
    prompt = getattr(request, "prompt", "") or ""
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
def model_visible_context_tokens(agent: object, params: object, prompt: object) -> int:
    _protocol, raw, _components, surface_fingerprint = _model_visible_context_components(
        agent,
        params,
        prompt,
    )
    return _provider_calibrated_context_tokens(
        agent,
        params,
        raw,
        context_surface_fingerprint=surface_fingerprint,
    )


# LLM: A successful provider call is the only authority allowed to calibrate the estimator. The
# numeric observation is copied to the exact ConversationThread with a compact-generation CAS so
# a background wake can reuse it; provider errors and synthetic responses never create a baseline.
# 函数用途: 记住并持久化上一次真实模型调用的“本地估算/厂商实际”对照，供本轮和后续后台唤醒减少误压缩。
def record_provider_context_observation(
    agent: object,
    params: object,
    *,
    raw_estimated_tokens: int,
    context_surface_fingerprint: str,
    response: object,
) -> bool:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return False
    if str(getattr(response, "runtime_status", "") or "").strip().lower() == (
        "context_overflow"
    ):
        return False
    observed = provider_visible_input_token_usage(response)
    raw = max(0, int(raw_estimated_tokens or 0))
    if observed is None or observed <= 0 or raw <= 0:
        state.pop(_PROVIDER_CONTEXT_OBSERVATION_KEY, None)
        _persist_provider_context_observation(agent, params, {})
        return False
    fingerprint = str(context_surface_fingerprint or "").strip()
    if not fingerprint:
        state.pop(_PROVIDER_CONTEXT_OBSERVATION_KEY, None)
        return False
    observation = {
        "schema": _PROVIDER_CONTEXT_OBSERVATION_SCHEMA,
        "raw_estimated_tokens": raw,
        "provider_input_tokens": max(1, int(observed)),
        "context_surface_fingerprint": fingerprint,
        "observed_at": time.time(),
    }
    state[_PROVIDER_CONTEXT_OBSERVATION_KEY] = {
        **observation,
        "_calibration_scope": _CURRENT_RUN_CALIBRATION_SCOPE,
    }
    _persist_provider_context_observation(agent, params, observation)
    return True


# LLM: Any history rewrite breaks the current-run append-only delta. The canonical Compact CAS
# clears the durable thread field; this helper removes the in-memory copy only after commit.
# 函数用途: 在真正改写模型历史后清除本轮旧校准点，下一次真实调用重新建立基线。
def invalidate_provider_context_observation(params: object) -> bool:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return False
    return state.pop(_PROVIDER_CONTEXT_OBSERVATION_KEY, None) is not None


# LLM: Current-run observations use 会话运行时 provider baseline plus append-only local growth.
# A reconstructed background slice may have replaced raw pairs with the same-generation handoff;
# for that durable observation use a conservative ratio (never below 50%) and charge all positive
# raw growth at full price. Fingerprint/generation mismatches always fall back to the raw estimate.
# 函数用途: 用服务端实际 token 校准本轮追加内容，并让后台唤醒在相同请求表面下安全复用会话级基线。
def _provider_calibrated_context_tokens(
    agent: object,
    params: object,
    raw_tokens: int,
    *,
    context_surface_fingerprint: str,
) -> int:
    raw = max(0, int(raw_tokens or 0))
    observation = _provider_context_observation(
        agent,
        params,
        context_surface_fingerprint=context_surface_fingerprint,
    )
    if not observation:
        return raw
    try:
        observed_raw = int(observation.get("raw_estimated_tokens") or 0)
        provider_input = int(observation.get("provider_input_tokens") or 0)
    except (TypeError, ValueError):
        return raw
    if observed_raw <= 0 or provider_input <= 0:
        return raw
    if str(observation.get("_calibration_scope") or "") != _DURABLE_CALIBRATION_SCOPE:
        if raw < observed_raw:
            return raw
        return provider_input + (raw - observed_raw)
    conservative_ratio_input = max(provider_input, (observed_raw + 1) // 2)
    scaled = (raw * conservative_ratio_input + observed_raw - 1) // observed_raw
    if raw < observed_raw:
        return scaled
    return max(scaled, provider_input + (raw - observed_raw))


# LLM: Hydration reads only the exact structured agent/conversation thread id and validates the
# compact generation plus stable request-surface fingerprint. Summary prose, cwd and prompt text
# never select another observation, and a corrupt/missing record simply restores raw estimation.
# 函数用途: 从当前会话线程按需加载上一次模型真实输入量；每个请求表面每轮最多查一次磁盘。
def _provider_context_observation(
    agent: object,
    params: object,
    *,
    context_surface_fingerprint: str,
) -> dict[str, object]:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return {}
    fingerprint = str(context_surface_fingerprint or "").strip()
    current = state.get(_PROVIDER_CONTEXT_OBSERVATION_KEY)
    if _provider_observation_matches(current, fingerprint):
        return current
    hydrated = state.get(_PROVIDER_CONTEXT_HYDRATION_KEY)
    if not isinstance(hydrated, set):
        hydrated = set()
        state[_PROVIDER_CONTEXT_HYDRATION_KEY] = hydrated
    if fingerprint in hydrated:
        return {}
    hydrated.add(fingerprint)
    _store, _thread_id, thread = _provider_observation_thread(agent, params)
    if thread is None:
        return {}
    observation = getattr(thread, "provider_context_observation", None)
    if not _provider_observation_matches(observation, fingerprint):
        return {}
    try:
        recorded_generation = int(observation.get("compact_generation") or 0)
        current_generation = int(getattr(thread, "compact_generation", 0) or 0)
    except (TypeError, ValueError):
        return {}
    if recorded_generation != current_generation:
        return {}
    durable = {**dict(observation), "_calibration_scope": _DURABLE_CALIBRATION_SCOPE}
    state[_PROVIDER_CONTEXT_OBSERVATION_KEY] = durable
    return durable


# LLM: Durable observation writes are fenced by the thread compact generation and are best-effort
# telemetry after a successful provider response; failure must not turn a good model call into a
# failed user turn. The store owns atomicity and deliberately does not advance thread recency.
# 函数用途: 把校准值写入当前主/子代理的精确会话线程；没有会话或发生并发压缩时静默放弃。
def _persist_provider_context_observation(
    agent: object,
    params: object,
    observation: dict[str, object],
) -> bool:
    store, thread_id, thread = _provider_observation_thread(agent, params)
    updater = getattr(getattr(store, 'threads', None), 'update_provider_context_observation', None)
    if thread is None or not thread_id or not callable(updater):
        return False
    generation = max(0, int(getattr(thread, "compact_generation", 0) or 0))
    payload = dict(observation)
    if payload:
        payload["compact_generation"] = generation
    try:
        updated = updater(
            thread_id,
            payload,
            expected_compact_generation=generation,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return (
        max(0, int(getattr(updated, "compact_generation", 0) or 0)) == generation
        and dict(getattr(updated, "provider_context_observation", {}) or {}) == payload
    )


# LLM: Agent thread id wins for delegated runs; otherwise the durable foreground conversation id
# is used. Both are host-authored task attributes, never values parsed from user/model language.
# 函数用途: 定位当前模型调用所属的唯一会话记录，供校准读取和保存。
def _provider_observation_thread(
    agent: object,
    params: object,
) -> tuple[object | None, str, object | None]:
    attributes = getattr(params, "task_attributes", None)
    attributes = attributes if isinstance(attributes, dict) else {}
    thread_id = str(
        attributes.get("agent_thread_id")
        or attributes.get("conversation_thread_id")
        or ""
    ).strip()
    store = getattr(agent, "conversation_store", None)
    loader = getattr(getattr(store, 'threads', None), 'load_report', None)
    if not thread_id or not callable(loader):
        return store, thread_id, None
    try:
        thread, error = loader(thread_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return store, thread_id, None
    return store, thread_id, thread if error is None else None


# LLM: Validation is intentionally closed: only this schema and an exact stable-surface digest may
# affect a compact decision. Private calibration scope is added after load and ignored here.
# 函数用途: 校验一条供应商上下文观测是否适用于当前模型请求表面。
def _provider_observation_matches(observation: object, fingerprint: str) -> bool:
    if not isinstance(observation, dict):
        return False
    return (
        observation.get("schema") == _PROVIDER_CONTEXT_OBSERVATION_SCHEMA
        and bool(fingerprint)
        and str(observation.get("context_surface_fingerprint") or "") == fingerprint
    )


# LLM: Estimation shares the real source-keyed prompt/IR projection without committing it; neither
# repeated facts nor a volatile prompt fingerprint may inflate preflight or defeat calibration.
# 函数用途: 用实际发给模型的分段投影估算占比，预检查不写 IR，动态状态也不混入稳定指纹。
def _model_visible_context_components(
    agent: object,
    params: object | None,
    prompt: object,
) -> tuple[str, int, dict[str, int], str]:
    system_instruction = provider_system_instruction(getattr(agent, "backend", None))
    if not native_tool_use_active(params):
        prompt_surface = {
            "system_instruction": system_instruction,
            "user_prompt": str(prompt or ""),
        }
        current = estimate_tokens(prompt_surface)
        return (
            "text",
            current,
            {
                "prompt_tokens": current,
                "messages_tokens": 0,
                "runtime_guidance_tokens": 0,
                "tool_schema_tokens": 0,
            },
            _stable_context_surface_fingerprint(
                agent,
                protocol="text",
                system_instruction=system_instruction,
                prompt_surface=str(prompt or ""),
                tools=None,
            ),
        )

    from ...backends.message_adapter import AnthropicMessageAdapter
    from ..tool_ir_guidance import unforwarded_runtime_guidance
    from ..tool_ir_history import project_native_prompt_history

    projected_prompt, history = project_native_prompt_history(params, prompt)
    current_messages = (
        AnthropicMessageAdapter().to_provider_messages(history)
        if history
        else []
    )
    messages = [
        *[
            item
            for item in list(getattr(params, "provider_history_messages", None) or [])
            if isinstance(item, dict)
        ],
        *current_messages,
    ]
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
    provider_prompt = _native_provider_prompt_adjunct(projected_prompt)
    payload = {
        "system_instruction": system_instruction,
        "prompt_adjunct": provider_prompt,
        "messages": messages,
        "pending_runtime_guidance": guidance,
        "tools": tools,
    }
    current = estimate_tokens(payload)
    components = _normalize_context_component_tokens(
        current,
        (
            (
                "prompt_tokens",
                {
                    "system_instruction": system_instruction,
                    "prompt_adjunct": provider_prompt,
                },
            ),
            ("messages_tokens", messages),
            ("runtime_guidance_tokens", guidance),
            ("tool_schema_tokens", tools),
        ),
    )
    return (
        "native",
        current,
        components,
        _stable_context_surface_fingerprint(
            agent,
            protocol="native",
            system_instruction=system_instruction,
            prompt_surface=provider_prompt,
            tools=tools,
        ),
    )


# LLM: This digest names only byte-stable provider request surfaces. Conversation messages,
# current-turn tool results and runtime guidance remain append-only variable input and are excluded;
# changing the backend, model, system prefix, stable prompt adjunct or tool schema invalidates reuse.
# 函数用途: 为跨后台轮次的模型校准生成稳定指纹，只保存哈希而不把提示词或工具定义写进线程状态。
def _stable_context_surface_fingerprint(
    agent: object,
    *,
    protocol: str,
    system_instruction: str,
    prompt_surface: str,
    tools: object,
) -> str:
    backend = getattr(agent, "backend", None)
    payload = {
        "backend": str(getattr(backend, "name", "") or ""),
        "model": str(
            getattr(backend, "model_name", "")
            or getattr(getattr(agent, "config", None), "model_name", "")
            or ""
        ),
        "protocol": str(protocol or ""),
        "system_instruction": str(system_instruction or ""),
        "prompt_surface": str(prompt_surface or ""),
        "tools": tools if isinstance(tools, list) else [],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# LLM: CacheStructuredPrompt keeps the canonical current user turn in its diagnostic string, but
# native adapters send that turn through messages. Estimation must omit only that typed duplicate;
# plain strings and every stable/dynamic prompt adjunct remain counted exactly once.
# 函数用途: 取得原生后端实际发送的非消息提示部分，避免状态条和 Compact 误把当前任务算两遍。
def _native_provider_prompt_adjunct(prompt: object) -> str:
    layout = prompt_cache_layout(prompt)
    if layout is None:
        return str(prompt or "")
    return "\n\n".join(
        part
        for part in (
            layout.stable_prefix,
            layout.stable_user_prefix,
            layout.volatile_suffix,
        )
        if part
    )


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


# LLM: Provider calibration changes only the total, not the private category payloads. Rescale the
# already-normalized numeric shares with deterministic largest-remainder allocation so the public
# component sum remains exactly equal to current_tokens without exposing any source content.
# 函数用途: 将 prompt/messages/tools 等原始占比缩放到校准后的总 token，保证状态条加总一致。
def _rescale_context_components(
    total_tokens: int,
    components: dict[str, int],
) -> dict[str, int]:
    keys = (
        "prompt_tokens",
        "messages_tokens",
        "runtime_guidance_tokens",
        "tool_schema_tokens",
    )
    total = max(0, int(total_tokens or 0))
    weights = [max(0, int(components.get(key, 0) or 0)) for key in keys]
    weight_total = sum(weights)
    if weight_total <= 0:
        return {key: total if index == 0 else 0 for index, key in enumerate(keys)}
    allocated = [weight * total // weight_total for weight in weights]
    remainder = total - sum(allocated)
    order = sorted(
        range(len(keys)),
        key=lambda index: (-(weights[index] * total % weight_total), index),
    )
    for index in order[:remainder]:
        allocated[index] += 1
    return {key: allocated[index] for index, key in enumerate(keys)}


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
    agent: object, params: object, current_prompt: object
) -> bool:
    policy = runtime_compact_policy(
        agent,
        save=_params_save_enabled(agent, params),
        context_scope=str(getattr(params, "context_scope", "") or "default"),
        task_attributes=getattr(params, "task_attributes", None),
    )
    if not policy.allow_persistent_apply:
        return False
    if not _has_previous_tool_context(params):
        return False
    threshold = int(policy.trigger_tokens or 0)
    if threshold <= 0:
        return False
    # EXEC-16: native 协议下工具结果在 IR messages 里、不在 prompt 文本里——
    # estimate_tokens(prompt) 恒为静态前缀(≈9K), 永远到不了阈值 → compact 永不触发
    # (ma 双线 36 轮仍 archive_events=0 实锤)。改用与 preflight 同口径的
    # model_visible_context_tokens: native 下含 IR messages/tools/guidance。
    return model_visible_context_tokens(agent, params, current_prompt or "") >= threshold


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
    "invalidate_provider_context_observation",
    "preflight_context_pressure_response",
    "record_provider_context_observation",
    "safe_inline_tool_result_tokens",
    "should_compact_before_more_tool_output",
]
