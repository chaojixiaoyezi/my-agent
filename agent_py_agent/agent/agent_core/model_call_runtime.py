# LLM: Model-call runtime bridges provider generation with structured timing ledgers.
# 模块用途: 记录模型调用 started/first_token/finished/timeout，并给动态超时提供运行时估算。

from __future__ import annotations

import hashlib
from typing import Any

from ..contracts.model_call_ledger import (
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)
from ..memory_archive import estimate_tokens
from .model_call_monitor import (
    FirstTokenTimeoutOptions,
    FirstTokenTimeoutParams,
    estimate_first_token_timeout,
    is_cache_suspected,
)
from .tool_stream_boundary import ToolBoundaryChunkFilter


# LLM: start_model_call_record records provider-call facts before network/model work begins.
# 函数用途: 创建本轮模型调用账本记录，并返回 call_id 与首 token 超时估算。
def start_model_call_record(request: object) -> tuple[ModelCallLedger, str, object]:
    ledger = model_call_ledger(getattr(request, "agent", None))
    prompt = str(getattr(request, "prompt", "") or "")
    input_tokens = estimate_tokens(prompt)
    estimate = estimate_first_token_timeout(
        FirstTokenTimeoutParams(
            input_tokens=input_tokens,
            ledger=ledger,
            options=first_token_timeout_options(getattr(request, "agent", None)),
        )
    )
    call_id = model_call_id(request, input_tokens)
    params = getattr(request, "params", None)
    backend = getattr(getattr(request, "agent", None), "backend", None)
    ledger.started(
        ModelCallStartedParams(
            call_id=call_id,
            backend=str(getattr(backend, "name", "") or ""),
            model=model_name(getattr(request, "agent", None)),
            input_tokens=input_tokens,
            output_tokens_estimate=max_output_tokens(getattr(request, "agent", None)),
            request_id=str(getattr(params, "request_id", "") or ""),
            run_id=str(getattr(params, "run_id", "") or ""),
            metadata={
                "tool_rounds": getattr(request, "tool_rounds", 0),
                "task_id": str(getattr(params, "task_id", "") or ""),
                "first_token_timeout_estimate": estimate.to_dict(),
            },
        )
    )
    return ledger, call_id, estimate


# LLM: observed_chunk_filter records first streamed output before delegating stream guards.
# 函数用途: 包装 on_chunk 回调，首个非空 chunk 到达时写 first_token 事件并继续原有流式边界。
def observed_chunk_filter(
    *,
    ledger: ModelCallLedger,
    call_id: str,
    chunk_filter: ToolBoundaryChunkFilter,
    first_token_estimate: object,
):
    seen_first_token = False

    # LLM: _on_chunk is intentionally tiny because it runs inside provider streaming callbacks.
    # 函数用途: 在不改变原流式输出行为的前提下，记录首 token 时间和缓存疑似标记。
    def _on_chunk(chunk: str) -> None:
        nonlocal seen_first_token
        if chunk and not seen_first_token:
            seen_first_token = True
            record_model_call_first_token(ledger, call_id, chunk, first_token_estimate)
        chunk_filter(chunk)

    return _on_chunk


# LLM: record_model_call_first_token writes a cache-aware first-token event from structured timing facts.
# 函数用途: 读取账本 started_at 和当前 clock 计算首 token 延迟，不解析模型输出正文。
def record_model_call_first_token(
    ledger: ModelCallLedger,
    call_id: str,
    chunk: str,
    first_token_estimate: object,
) -> None:
    record = next((item for item in ledger.records() if item.call_id == call_id), None)
    if record is None:
        return
    latency = max(0.0, float(ledger.context.now()) - record.started_at)
    ledger.first_token(
        ModelCallFirstTokenParams(
            call_id=call_id,
            output_tokens_seen=estimate_tokens(chunk),
            cache_suspected=is_cache_suspected(
                input_tokens=record.input_tokens,
                first_token_latency_seconds=latency,
                estimate=first_token_estimate,
            ),
        )
    )


# LLM: record_model_call_finished stores completion facts after response normalization.
# 函数用途: 模型返回或流式大写入被系统转为恢复响应后，记录输出 token 估算。
def record_model_call_finished(ledger: ModelCallLedger, call_id: str, response: object) -> None:
    ledger.finished(ModelCallFinishParams(call_id=call_id, output_tokens=estimate_tokens(getattr(response, "text", ""))))


# LLM: record_model_call_timeout records timeout facts without reading provider error prose.
# 函数用途: provider wall timeout 发生时，把结构化超时阶段和预算写入账本。
def record_model_call_timeout(
    *,
    ledger: ModelCallLedger,
    call_id: str,
    timeout_seconds: float,
    timeout_stage: str,
) -> None:
    ledger.timeout(
        ModelCallTimeoutParams(
            call_id=call_id,
            timeout_seconds=timeout_seconds,
            timeout_stage=timeout_stage,
        )
    )


# LLM: model_call_ledger stores per-agent model timing facts without adding global state.
# 函数用途: 从 agent 取或创建模型调用账本；无法写回时仍返回临时账本保证运行不崩。
def model_call_ledger(agent: object) -> ModelCallLedger:
    existing = getattr(agent, "_model_call_ledger", None)
    if isinstance(existing, ModelCallLedger):
        return existing
    ledger = ModelCallLedger()
    try:
        agent._model_call_ledger = ledger
    except Exception:
        return ledger
    return ledger


# LLM: effective_model_request_timeout_seconds extends wall timeout only when dynamic timeout config exists.
# 函数用途: 保持旧 request_timeout 语义，同时允许大 prompt 根据首 token 估算抬高总等待预算。
def effective_model_request_timeout_seconds(agent: object, first_token_timeout_seconds: float) -> float:
    base_timeout = model_request_timeout_seconds(agent)
    if base_timeout <= 0:
        return 0.0
    if not has_dynamic_timeout_config(agent):
        return base_timeout
    return max(base_timeout, max(0.0, float(first_token_timeout_seconds)))


# LLM: model_request_timeout_seconds resolves the public request_timeout setting for the guard layer.
# 函数用途: 从 agent.config 或 backend 上读取 request_timeout；无效或关闭时返回 0 表示不启用总时长保护。
def model_request_timeout_seconds(agent: object) -> float:
    config = getattr(agent, "config", None)
    raw = getattr(config, "request_timeout", None)
    if raw is None:
        raw = getattr(getattr(agent, "backend", None), "request_timeout", 0)
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return timeout if timeout > 0 else 0.0


# LLM: has_dynamic_timeout_config prevents fake agents from accidentally changing legacy timeout behavior.
# 函数用途: 只有配置对象显式带 dynamic_timeout 字段时，才启用模型首 token 动态预算扩展。
def has_dynamic_timeout_config(agent: object) -> bool:
    config = getattr(agent, "config", None)
    return any(hasattr(config, name) for name in ("dynamic_timeout_min", "dynamic_timeout_max", "dynamic_timeout_safety_margin"))


# LLM: first_token_timeout_options maps existing dynamic timeout config into model-call estimation.
# 函数用途: 复用已有 dynamic_timeout_min/max/safety_margin，避免新增一堆用户配置。
def first_token_timeout_options(agent: object) -> FirstTokenTimeoutOptions:
    config = getattr(agent, "config", None)
    return FirstTokenTimeoutOptions(
        safety_margin=float_config(config, "dynamic_timeout_safety_margin", 1.5),
        min_timeout_seconds=float_config(config, "dynamic_timeout_min", 5.0),
        max_timeout_seconds=float_config(config, "dynamic_timeout_max", 120.0),
    )


# LLM: model_call_id derives a stable operation id from structured run fields and prompt hash.
# 函数用途: 生成账本 call_id，避免用自然语言摘要作为模型调用事实标识。
def model_call_id(request: object, input_tokens: int) -> str:
    params = getattr(request, "params", None)
    seed = "|".join(
        [
            str(getattr(params, "request_id", "") or ""),
            str(getattr(params, "run_id", "") or ""),
            str(getattr(params, "task_id", "") or ""),
            str(getattr(request, "tool_rounds", 0)),
            str(input_tokens),
            hashlib.sha256(str(getattr(request, "prompt", "") or "").encode("utf-8")).hexdigest()[:16],
        ]
    )
    return f"model-call:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


# LLM: model_name reads model identity from backend/config fields used across adapters.
# 函数用途: 提取模型名称用于账本统计；缺失时返回空字符串。
def model_name(agent: object) -> str:
    backend = getattr(agent, "backend", None)
    config = getattr(agent, "config", None)
    return str(getattr(backend, "model_name", "") or getattr(config, "model_name", "") or getattr(config, "model", "") or "")


# LLM: max_output_tokens keeps output estimate aligned with backend/config without requiring provider usage.
# 函数用途: 读取 max_tokens 作为输出 token 预算估计，缺失或无效时返回 0。
def max_output_tokens(agent: object) -> int:
    raw = getattr(getattr(agent, "backend", None), "max_tokens", None)
    if raw is None:
        raw = getattr(getattr(agent, "config", None), "max_tokens", 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


# LLM: float_config reads numeric config fields with safe fallback.
# 函数用途: 把配置字段转换为 float；缺失或无效时返回默认值。
def float_config(config: object, name: str, default: float) -> float:
    try:
        return float(getattr(config, name, default))
    except (TypeError, ValueError):
        return default


__all__ = [
    "effective_model_request_timeout_seconds",
    "model_request_timeout_seconds",
    "observed_chunk_filter",
    "record_model_call_finished",
    "record_model_call_timeout",
    "start_model_call_record",
]
