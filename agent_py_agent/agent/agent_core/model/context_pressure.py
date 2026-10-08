# LLM: context-pressure 的展示和阈值只来自原策略；消息清扫、引导和 ToolChoice 与出站共源，纯计量不读取宿主或校准。
# 模块用途: 估算即将发送的模型输入并给出结构化压缩信号，保持输出预留、运行期校准及纯投影的边界。
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from ...backends import ModelResponse, is_provider_context_window_error
from ...backends.request_content import classify_nontext_content
from ...backends.tool_protocol_adapter import tools_for_choice
from ...conversation.compact_calibration import (
    CALIBRATION_SCOPE_CURRENT_RUN,
    CALIBRATION_SCOPE_DURABLE_THREAD,
    CALIBRATION_SCOPE_OWNER_RATIO,
    CompactRequestCalibration,
)
from ...conversation.compact_media_policy import configured_media_policy, media_token_reserve
from ...conversation.compact_progress import (
    COMPACT_TRIGGER_PREFLIGHT,
    COMPACT_TRIGGER_TOOL_CONTEXT_OVERFLOW,
)
from ...memory_archive import estimate_tokens
from ...model_guidance import provider_system_instruction
from ...prompting_parts.cache_layout import prompt_cache_layout
from ...settings.defaults import configured_context_window_tokens
from ..native_tool_protocol import (
    model_turn_tool_choice,
    native_tool_use_active,
    resolve_native_tools,
)
from ..runtime.context_compactor import runtime_compact_policy
from ..tool_request_projection import ToolLoopRequestProjection, compact_request_source_supported
from .context_calibration_carry import read_owner_ratio, record_owner_ratio
from .usage import provider_visible_input_token_usage

_PROVIDER_CONTEXT_OBSERVATION_KEY = "_provider_context_observation"
_PROVIDER_CONTEXT_HYDRATION_KEY = "_provider_context_observation_hydrated_surfaces"
_PROVIDER_CONTEXT_OBSERVATION_SCHEMA = "provider_context_observation.v3"
# 作用域词表的唯一权威在 conversation.compact_calibration；这里只是本模块的短别名。
_DURABLE_CALIBRATION_SCOPE = CALIBRATION_SCOPE_DURABLE_THREAD
_CURRENT_RUN_CALIBRATION_SCOPE = CALIBRATION_SCOPE_CURRENT_RUN
_OWNER_RATIO_CALIBRATION_SCOPE = CALIBRATION_SCOPE_OWNER_RATIO
# 本轮内 owner 比值缓存的内存副本（键 → 记录或空字典），每个分词身份每轮最多读一次盘，本轮成功观测后就地刷新。
_PROVIDER_CONTEXT_OWNER_RATIO_KEY = "_provider_context_owner_ratio"
# 压缩提交后派生观测的结构化标记：它不是供应商实测，而是“已接受候选按同一观测折算”的本轮基线。
_COMPACT_CANDIDATE_OBSERVATION_BASIS = "compact_candidate"


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
    # 这次调用实际用的模型名与思考档位（"off" 表示关、"auto" 表示交给模型默认），只给状态行显示；空串表示未知。
    model_name: str = ""
    reasoning_level: str = ""

    # LLM: Public serialization exposes only bounded numeric facts, two short display labels (model name, reasoning
    # level) and the frozen schema id; prompts, messages, tool definitions, and guidance content must never enter the UI event.
    # 函数用途: 把上下文快照转换成可安全发送给 TUI 的结构化字典，不包含任何正文。
    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema": "model_visible_context_usage.v1",
            "estimated": True,
            "model_name": self.model_name,
            "reasoning_level": self.reasoning_level,
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
        model_name=str(getattr(getattr(agent, "backend", None), "model_name", "") or "")[:80],
        reasoning_level=_display_reasoning_level(agent, params),
    )


# LLM: 与真实请求同一解析（settings.reasoning_effort.run_reasoning_level + backends.reasoning_control.reasoning_request_values，
#   线程档位优先、其次全局默认、再按后端控制方式降成实际发送值）：关思考记 "off"，发了档位记档位，交给模型默认记 "auto"。
#   只给状态行显示，读不到按空串，绝不因此影响请求。
# 函数用途: 算出这次调用"思考开的是啥"，和实际发给模型的一致。
def _display_reasoning_level(agent: object, params: object | None) -> str:
    from ...backends.reasoning_control import reasoning_request_values
    from ...settings.reasoning_effort import run_reasoning_level

    try:
        control = str(getattr(getattr(agent, "backend", None), "reasoning_control", "none") or "none")
        disabled, effort = reasoning_request_values(run_reasoning_level(agent, params), control, forced=False)
    except Exception:
        return ""
    return "off" if disabled else (effort or "auto")


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


# LLM: 输入阈值仍只按当前 Context 算；内置 HTTP 后端有明确共享窗口和实际发送的
# max_tokens 时，另检查本次请求可容纳性。OAuth Responses 不发送输出上限，不能猜测。
# 请求含 unknown 非文本块、或媒体策略为 off 且含媒体时，压缩链不可用，压缩点不再是客观门槛，此时只守窗口与输出预留的硬上限；
# 已知媒体在策略不为 off 时压缩链可用（归档引用/随图摘要），门槛照常取压缩点。与 compact_request_recovery 的 _automatic_noop/select 同口径，改动须同步。
# 返回的 runtime_source 是结构化触发来源（compact_progress.COMPACT_TRIGGER_*）：工具窗口溢出与普通预检越线分开报，
# 下游不得从 detail 文本区分。
# 函数用途: 每次模型请求前检查输入压缩点和已知协议容量，超出时沿原 Compact 链恢复。
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
            source=COMPACT_TRIGGER_TOOL_CONTEXT_OVERFLOW,
            prompt_tokens=prompt_tokens,
            detail=(
                "tool_context_window_overflow=true "
                f"omitted_count={overflow.get('omitted_count', 0)} "
                f"original_chars={overflow.get('original_chars', 0)}"
            ),
        )
    threshold = policy.trigger_tokens
    compact_chain_available = params is None or compact_request_source_supported(
        params, media_policy=configured_media_policy(getattr(request, "agent", None)),
    )
    if not policy.allow_persistent_apply:
        threshold = window
    elif not compact_chain_available:
        # 压缩链对此请求不可用，在压缩点失败不是客观事实；越过窗口时仍由强制恢复给出结构化拒绝。
        threshold = window
    output_reserve = _known_shared_window_output_reserve(getattr(request, "agent", None))
    request_ceiling = model_request_input_ceiling(request.agent, window)
    if prompt_tokens < threshold and prompt_tokens < request_ceiling:
        return None
    reason = (
        f"request_output_reserve={output_reserve} request_input_ceiling={request_ceiling} "
        if prompt_tokens >= request_ceiling and output_reserve else ""
    )
    capacity = "" if compact_chain_available else "compact_capacity=non_text "
    return context_pressure_response(
        request,
        source=COMPACT_TRIGGER_PREFLIGHT,
        prompt_tokens=prompt_tokens,
        detail=(
            f"model_visible_tokens={prompt_tokens} "
            f"context_window={window} compact_threshold={threshold} {capacity}{reason}".strip()
        ),
    )


# LLM: 只有内置 HTTP 后端的配置共享窗口与实际发送的正数输出上限同时可证时才预留；“配置窗口可证”即
# model_context_window_tokens 填了正整数（原 model_context_window_explicit 已并入）；
# OAuth Responses 会移除 max_output_tokens，未知/仅 input limit 元数据不得当成共享窗口。
# 函数用途: 读取本轮可证明会进入请求体的输出上限，无法证明时保持原输入预检。
def _known_shared_window_output_reserve(agent: object) -> int:
    from ...backends.http import HttpBackend

    backend = getattr(agent, "backend", None)
    config = getattr(agent, "config", None)
    if not isinstance(backend, HttpBackend):
        return 0
    if configured_context_window_tokens(config) <= 0:
        return 0
    if str(getattr(backend, "name", "")) == "openai_responses":
        auth_ref = getattr(backend, "auth_ref", None)
        if isinstance(auth_ref, dict) and auth_ref.get("mode") == "chatgpt":
            return 0
    try:
        requested_output = int(getattr(backend, "max_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, requested_output)


# LLM: 普通生成与 transcript 候选共用已知共享窗口及实际输出 cap；未知协议不猜预留，返回值不改变 Context 的输入占用显示。
# 函数用途: 计算本请求输入必须严格低于的容量边界，阻止压缩后仍因输出预留不足立即再次超窗。
def model_request_input_ceiling(agent: object, context_window_tokens: int) -> int:
    return max(0, context_window_tokens - _known_shared_window_output_reserve(agent))


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
    _carry_owner_ratio(agent, params, observation)
    return True


# LLM: 线程观测的派生副本：按本次请求的分词身份写 owner 级比值缓存，并刷新本轮内存副本；写失败只影响缓存，不影响本次调用。
# 函数用途: 把这次成功调用的“本地估算 / 供应商实际”留给同一模型的新会话、压缩后与换表面后的第一次调用。
def _carry_owner_ratio(agent: object, params: object, observation: dict[str, object]) -> None:
    if not _owner_ratio_carry_enabled(agent):
        return
    raw, provider = int(observation["raw_estimated_tokens"]), int(observation["provider_input_tokens"])
    key = _tokenizer_identity_key(agent, _request_protocol(params))
    if record_owner_ratio(agent, key, raw, provider):
        cache = _owner_ratio_cache(params)
        if cache is not None:
            cache[key] = {"raw_estimated_tokens": raw, "provider_input_tokens": provider, "observed_at": time.time()}


# LLM: Any history rewrite breaks the current-run append-only delta. The canonical Compact CAS
# clears the durable thread field; this helper removes the in-memory copy only after commit.
# 函数用途: 在真正改写模型历史后清除本轮旧校准点，下一次真实调用重新建立基线。
def invalidate_provider_context_observation(params: object) -> bool:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return False
    return state.pop(_PROVIDER_CONTEXT_OBSERVATION_KEY, None) is not None


# LLM: 宿主边界（PreparedCompactRecovery.select）在冻结完整请求后调用一次：沿用 _provider_context_observation 的
#   本轮/耐久水合规则（同 fingerprint；耐久观测须同压缩代次），把可用观测冻结成纯数据交给候选门、压缩前计量和
#   自动判定。找不到、表面或代次不符、数字不可用都返回 None，调用方保持原始口径，不猜系数。水合是与预检相同的
#   一次读盘副作用；投影本身仍是纯函数。
# 函数用途: 为一次压缩恢复冻结“本地估算 / 供应商实际”的校准事实。
def frozen_compact_request_calibration(
    agent: object,
    params: object,
    *,
    context_surface_fingerprint: str,
) -> CompactRequestCalibration | None:
    fingerprint = str(context_surface_fingerprint or "").strip()
    observation = _provider_context_observation(agent, params, context_surface_fingerprint=fingerprint)
    if not observation:
        return None
    try:
        observed_raw = int(observation.get("raw_estimated_tokens") or 0)
        provider_input = int(observation.get("provider_input_tokens") or 0)
        recorded_generation = int(observation.get("compact_generation") or 0)
    except (TypeError, ValueError):
        return None
    if observed_raw <= 0 or provider_input <= 0:
        return None
    scope = str(observation.get("_calibration_scope") or _CURRENT_RUN_CALIBRATION_SCOPE)
    if scope != _DURABLE_CALIBRATION_SCOPE:
        _store, _thread_id, thread = _provider_observation_thread(agent, params)
        recorded_generation = max(0, int(getattr(thread, "compact_generation", 0) or 0))
    return CompactRequestCalibration(
        raw_estimated_tokens=observed_raw,
        provider_input_tokens=provider_input,
        context_surface_fingerprint=fingerprint,
        compact_generation=recorded_generation,
        calibration_scope=scope,
    )


# LLM: 压缩提交后由恢复宿主调用：已接受候选的原始计量与校准值成为下一次真实预检的本轮基线（同 fingerprint，仍是
#   provider + 追加增量口径），让“候选通过 → 提交 → 下一次预检”说同一种数，而不是清掉观测后按原始值立刻再压一次。
#   写入的是派生观测（basis/derived_from_generation 结构化标记），只存本轮内存状态、不持久化到线程；下一次真实
#   供应商响应照常覆盖。没有校准的提交仍走 invalidate_provider_context_observation。
# 函数用途: 压缩提交后把本轮校准基线改写成接受基准，避免刚接受的候选被另一口径立刻判为超限。
def rebase_provider_context_observation(
    params: object,
    *,
    calibration: CompactRequestCalibration | None,
    raw_estimated_tokens: int,
    calibrated_tokens: int,
) -> bool:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict) or calibration is None:
        return False
    raw = max(0, int(raw_estimated_tokens or 0))
    calibrated = max(0, int(calibrated_tokens or 0))
    fingerprint = str(calibration.context_surface_fingerprint or "").strip()
    if raw <= 0 or calibrated <= 0 or not fingerprint:
        return False
    state[_PROVIDER_CONTEXT_OBSERVATION_KEY] = {
        "schema": _PROVIDER_CONTEXT_OBSERVATION_SCHEMA,
        "raw_estimated_tokens": raw,
        "provider_input_tokens": calibrated,
        "context_surface_fingerprint": fingerprint,
        "observed_at": time.time(),
        "basis": _COMPACT_CANDIDATE_OBSERVATION_BASIS,
        "derived_from_generation": max(0, int(calibration.compact_generation or 0)),
        "_calibration_scope": _CURRENT_RUN_CALIBRATION_SCOPE,
    }
    return True


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
    scope = str(observation.get("_calibration_scope") or "")
    if scope == _OWNER_RATIO_CALIBRATION_SCOPE:
        # 别的会话/表面的观测：两边原始量不在同一基线上，只能按比例折算，不能加减。
        return _conservative_ratio_tokens(raw, observed_raw, provider_input)
    if scope != _DURABLE_CALIBRATION_SCOPE:
        if raw < observed_raw:
            return raw
        return provider_input + (raw - observed_raw)
    scaled = _conservative_ratio_tokens(raw, observed_raw, provider_input)
    if raw < observed_raw:
        return scaled
    return max(scaled, provider_input + (raw - observed_raw))


# LLM: 与 compact_calibration.calibrated_compact_request_tokens 的比例口径相同：ceil(raw × max(provider, ceil(observed/2)) / observed)，
#   折算比例不低于 50%，估算偏低时比例大于 1 照样放大（保守）。纯函数。
# 函数用途: 按一次观测的“供应商实际 / 本地估算”比例折算本次原始估算。
def _conservative_ratio_tokens(raw: int, observed_raw: int, provider_input: int) -> int:
    conservative_ratio_input = max(provider_input, (observed_raw + 1) // 2)
    return (raw * conservative_ratio_input + observed_raw - 1) // observed_raw


# LLM: 取校准观测的唯一入口（预检、状态条快照、压缩冻结都经这里）：先本轮/线程的精确观测（同表面指纹、同压缩代次），
#   都对不上时才回落 owner 级按分词身份的比值（作用域 owner_ratio，只按比例折算）。换模型、换连接地址或鉴权方式时
#   分词身份也变，拿不到就回到原始估算，不借别的模型的比值。
# 函数用途: 为本次请求找一条可用的“本地估算 / 供应商实际”观测，找不到返回空字典。
def _provider_context_observation(
    agent: object,
    params: object,
    *,
    context_surface_fingerprint: str,
) -> dict[str, object]:
    exact = _exact_provider_context_observation(agent, params, context_surface_fingerprint=context_surface_fingerprint)
    if exact:
        return exact
    cache = _owner_ratio_cache(params)
    if cache is None or not _owner_ratio_carry_enabled(agent):
        return {}
    key = _tokenizer_identity_key(agent, _request_protocol(params))
    if key not in cache:
        cache[key] = read_owner_ratio(agent, key)
    entry = cache[key]
    return {**entry, "_calibration_scope": _OWNER_RATIO_CALIBRATION_SCOPE} if entry else {}


# LLM: live_archive_state 里的本轮 owner 比值副本；没有本轮状态（伪 params）时返回 None，调用方按“没有缓存”处理。
# 函数用途: 取得本轮内 owner 比值缓存的内存副本。
def _owner_ratio_cache(params: object) -> dict[str, dict] | None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return None
    cache = state.get(_PROVIDER_CONTEXT_OWNER_RATIO_KEY)
    if not isinstance(cache, dict):
        cache = {}
        state[_PROVIDER_CONTEXT_OWNER_RATIO_KEY] = cache
    return cache


# LLM: 开关 memory_context_calibration_carry_enabled（默认开）；关掉时 owner 比值缓存既不读也不写，回到只用本轮/线程观测。
# 函数用途: 判断是否允许跨会话沿用 owner 级校准比值。
def _owner_ratio_carry_enabled(agent: object) -> bool:
    return bool(getattr(getattr(agent, "config", None), "memory_context_calibration_carry_enabled", True))


# 函数用途: 本次请求走原生工具协议还是纯文本协议，与 _model_visible_context_components 同一判定。
def _request_protocol(params: object) -> str:
    return "native" if native_tool_use_active(params) else "text"


# LLM: Hydration reads only the exact structured agent/conversation thread id and validates the
# compact generation plus stable request-surface fingerprint. Summary prose, cwd and prompt text
# never select another observation, and a corrupt/missing record simply restores raw estimation.
# 函数用途: 从当前会话线程按需加载上一次模型真实输入量；每个请求表面每轮最多查一次磁盘。
def _exact_provider_context_observation(
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


# LLM: 本入口准备宿主事实；投影后交唯一纯计量，原 native 清扫/引导/ToolChoice 与发送共源，不写 IR。
# 函数用途: 采集实际消息与工具展示后估算上下文；稳定指纹不含动态状态，校准仍归外层原合同。
def _model_visible_context_components(
    agent: object,
    params: object | None,
    prompt: object,
) -> tuple[str, int, dict[str, int], str]:
    system_instruction = provider_system_instruction(getattr(agent, "backend", None))
    if not native_tool_use_active(params):
        projection = ToolLoopRequestProjection(
            "ready", provider_prompt=str(prompt or ""), system_instruction=system_instruction,
        )
        current, components = projected_model_context_components(projection)
        return "text", current, components, request_projection_surface_fingerprint(agent, projection)

    from ..tool_ir_guidance import unforwarded_runtime_guidance
    from ..tool_ir_history import project_native_prompt_history, project_native_provider_messages

    projected_prompt, history = project_native_prompt_history(params, prompt)
    state = getattr(params, "live_archive_state", None)
    already_forwarded = (
        set(state.get("_forwarded_runtime_guidance", set()))
        if isinstance(state, dict)
        else set()
    )
    guidance = unforwarded_runtime_guidance(
        getattr(params, "tool_context", None),
        set(already_forwarded),
    )
    messages = project_native_provider_messages(
        history, prior_messages=getattr(params, "provider_history_messages", None),
        tool_context=getattr(params, "tool_context", None), forwarded_guidance=already_forwarded,
    )
    available_tools = resolve_native_tools(agent, params)
    choice = model_turn_tool_choice(params, available_tools)
    tools = tools_for_choice(available_tools, choice)
    provider_prompt = _native_provider_prompt_adjunct(projected_prompt)
    projection = ToolLoopRequestProjection(
        "ready", provider_prompt=projected_prompt, system_instruction=system_instruction,
        messages=messages, tools=tools or None, tool_choice=choice,
    )
    current, components = projected_model_context_components(
        projection, pending_runtime_guidance=guidance, media_token_reserve=media_token_reserve(),
    )
    return "native", current, components, request_projection_surface_fingerprint(agent, projection)


# LLM: 预检与恢复宿主共用的稳定表面指纹入口：从同一 ToolLoopRequestProjection 取协议、系统提示、稳定提示附件与
#   工具清单，与实际出站完全同源；冻结请求与预检算出同一指纹，观测才能在两边同时适用。只读，不写状态。
# 函数用途: 给一份完整请求投影算稳定请求表面指纹，供恢复宿主查找适用的供应商观测。
def request_projection_surface_fingerprint(agent: object, projection: ToolLoopRequestProjection) -> str:
    native = projection.messages is not None
    provider_prompt = projection.provider_prompt
    return _stable_context_surface_fingerprint(
        agent,
        protocol="native" if native else "text",
        system_instruction=str(projection.system_instruction or ""),
        prompt_surface=(
            _native_provider_prompt_adjunct(provider_prompt) if native else str(provider_prompt or "")
        ),
        tools=projection.tools if native else None,
    )


# LLM: 只消费已准备投影与分类引导，未知拒绝计数；不读宿主/校准、不改状态或发请求，provider组包和输出预留仍由调用方核验。
#   media_token_reserve 由调用方传入（常量 compact_media_policy.INPUT_MEDIA_TOKEN_RESERVE_TOKENS）：运输层会展开的已知图块按每块该值折进
#   messages_tokens 与总量（预检、_automatic_noop 与恢复候选计量同口径），0 表示不折。
# 函数用途: 以原 estimate_tokens 估算 system、prompt、原生消息和schema占比，并把已知图块按固定预留计入；
#   这不是供应商精确token或完整容量准入证明。
def projected_model_context_components(
    projection: ToolLoopRequestProjection, *, pending_runtime_guidance: object = (), media_token_reserve: int = 0,
) -> tuple[int, dict[str, int]]:
    if (projection.status != "ready" or projection.provider_prompt is None
            or projection.system_instruction is None):
        raise ValueError("complete model request projection required")
    native = projection.messages is not None
    prompt_surface = {
        "system_instruction": projection.system_instruction,
        "prompt_adjunct" if native else "user_prompt": (
            _native_provider_prompt_adjunct(projection.provider_prompt) if native else str(projection.provider_prompt)
        ),
    }
    payload = dict(prompt_surface)
    weights = {
        "prompt_tokens": estimate_tokens(prompt_surface),
        "messages_tokens": 0,
        "runtime_guidance_tokens": 0,
        "tool_schema_tokens": 0,
    }
    media_weight = 0
    if native:
        tools = projection.tools or []
        payload.update(messages=projection.messages, tools=tools)
        message_weight = estimate_tokens(projection.messages) if projection.messages else 0
        guidance_weight = (
            min(message_weight, estimate_tokens(pending_runtime_guidance)) if pending_runtime_guidance else 0
        )
        media_weight = _media_reserve_tokens(projection.messages, media_token_reserve)
        weights.update(
            messages_tokens=message_weight - guidance_weight + media_weight,
            runtime_guidance_tokens=guidance_weight,
            tool_schema_tokens=estimate_tokens(tools) if tools else 0,
        )
    total = estimate_tokens(payload) + media_weight
    return total, _rescale_context_components(total, weights)


# LLM: 只数 classify_nontext_content 认定的已知媒体块（顶层 user 行 local_file image/video，与运输层展开集合相同），
#   乘每块固定预留；不读图、不探视觉能力、不看压缩策略——图块不论策略如何都会进请求体。
# 函数用途: 把消息里的已知图块按每块预留 token 折进估算，避免多图上下文在预检和自动压缩判定里被低估。
def _media_reserve_tokens(messages: object, reserve: int) -> int:
    per_block = max(0, int(reserve or 0))
    if per_block <= 0 or not messages:
        return 0
    return classify_nontext_content(messages).media * per_block


# LLM: Connection fields from third-party or test backends may be opaque objects; never serialize
# their repr (which can expose secrets), and keep each unknown object distinct within this process.
# 函数用途: 将可证明的连接字段保留为 JSON 值，未知对象以进程内身份标记供指纹比较（这类对象只在本进程内可复用校准）。
def _connection_fact(value: object) -> object:
    if value is None or type(value) in (str, int, bool, float):
        return value
    if type(value) in (dict, list, tuple):
        try:
            json.dumps(value, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            pass
        else:
            return value
    return {"opaque_type": type(value).__qualname__, "object_id": id(value)}


# LLM: This digest is PERSISTED on the thread (provider_context_observation) and must stay equal across
# Gateway restarts for the same request surface. Conversation messages, current-turn tool results and
# runtime guidance remain append-only variable input and are excluded; changing the backend, connection
# profile, endpoint, auth mode, model, system prefix, stable prompt adjunct or tool schema invalidates
# reuse. Credentials, headers and any per-process salt (decision_policy.connection_revision) must never
# enter it: the former do not change tokenization, the latter made every restart drop calibration
# (TUI context number jumped ~28% high on each run's first call, 2026-09-30). test_runtime_context_pressure
# pins both properties (restart survival and credential rotation).
# 函数用途: 为跨回合、跨进程的模型校准生成稳定指纹，换连接地址/模型/鉴权方式或展示面时失效旧观测，只保存哈希。
def _stable_context_surface_fingerprint(
    agent: object,
    *,
    protocol: str,
    system_instruction: str,
    prompt_surface: str,
    tools: object,
) -> str:
    payload = {
        **_tokenizer_identity_payload(agent, protocol),
        "system_instruction": str(system_instruction or ""),
        "prompt_surface": str(prompt_surface or ""),
        "tools": tools if isinstance(tools, list) else [],
    }
    return _sha256_json(payload)


# LLM: 分词身份 = 后端名、模型名、协议（native/text）与不含凭据的连接身份；稳定表面指纹在它之上再加系统提示、稳定提示与工具。
#   两者共用这一份载荷，保证 owner 比值缓存的键与线程观测对“同一个模型”的判断一致。键名与取值不能改（会让已存观测全部失配）。
# 函数用途: 给出“同一个模型、同一种组包方式”的结构化身份。
def _tokenizer_identity_payload(agent: object, protocol: str) -> dict[str, object]:
    backend = getattr(agent, "backend", None)
    config = getattr(agent, "config", None)
    return {
        "backend": str(getattr(backend, "name", "") or ""),
        "model": str(getattr(backend, "model_name", "") or getattr(config, "model_name", "") or ""),
        "protocol": str(protocol or ""),
        "connection": _tokenizer_connection_identity(backend, config),
    }


# 函数用途: 分词身份的摘要，作为 owner 比值缓存的键（只存哈希）。
def _tokenizer_identity_key(agent: object, protocol: str) -> str:
    return _sha256_json(_tokenizer_identity_payload(agent, protocol))


# 函数用途: 按固定序列化规则对结构化载荷取 sha256 十六进制摘要。
def _sha256_json(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# LLM: 只收决定“同一请求算多少 token”的连接事实：模型档案、后端地址与鉴权方式（auth_ref 只取 mode）。
#   api_key、请求头、会话头不改变分词，且会随轮换变化，一律不进；也不能用进程加盐摘要，否则重启后校准全部失效。
#   新增字段前先确认它跨进程稳定且不含凭据，并同步 test_runtime_context_pressure 的重启与轮换用例。
# 函数用途: 给持久校准指纹提供跨进程稳定、不含密钥的连接身份。
def _tokenizer_connection_identity(backend: object, config: object) -> dict[str, object]:
    sources = getattr(config, "config_sources", None)
    model_source = sources.get("model_name", {}) if isinstance(sources, dict) else {}
    model_source = model_source if isinstance(model_source, dict) else {}
    return {
        "profile_id": str(model_source.get("profile_id") or ""),
        "backend_api_base": _connection_fact(getattr(backend, "api_base", None)),
        "backend_auth_mode": _auth_mode(getattr(backend, "auth_ref", None)),
        "model_backend": _connection_fact(getattr(config, "model_backend", None)),
        "config_api_base": _connection_fact(getattr(config, "api_base", None)),
        "config_auth_mode": _auth_mode(getattr(config, "model_auth_ref", None)),
    }


# LLM: auth_ref 可能带账号或令牌引用，只读结构化 mode 字符串；非 dict 一律视为未声明。
# 函数用途: 从鉴权引用里取出不含凭据的鉴权方式名。
def _auth_mode(auth_ref: object) -> str:
    if not isinstance(auth_ref, dict):
        return ""
    mode = auth_ref.get("mode")
    return mode if isinstance(mode, str) else ""


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


# LLM: 只对原始或已归一化数值权重做最大余数分摊，不读取正文；未校准和校准总量均须精确等于分类之和。
# 函数用途: 将 prompt/messages/tools 占比缩放到指定总 token，保持状态条分类加总一致。
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
    "frozen_compact_request_calibration",
    "rebase_provider_context_observation",
    "request_projection_surface_fingerprint",
    "model_visible_context_tokens",
    "model_request_input_ceiling",
    "invalidate_provider_context_observation",
    "preflight_context_pressure_response",
    "record_provider_context_observation",
    "safe_inline_tool_result_tokens",
    "should_compact_before_more_tool_output",
]
