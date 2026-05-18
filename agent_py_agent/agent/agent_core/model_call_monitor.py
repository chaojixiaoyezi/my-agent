# LLM: Model call monitor derives first-token timeout budgets from structured ledger facts.
# 模块用途: 根据模型调用账本的 token/latency/probe 样本估算首 token 超时和缓存疑似标记。

from __future__ import annotations

from dataclasses import dataclass

from ..contracts.model_call_ledger import ModelCallLedger, ModelCallRecord


# LLM: FirstTokenTimeoutOptions bundles operator-tunable timeout estimation constants.
# 类用途: 保存首 token 估算的 fallback 速率、安全边际和 clamp 边界。
@dataclass(frozen=True)
class FirstTokenTimeoutOptions:
    fallback_prefill_tokens_per_second: float = 400.0
    base_first_token_seconds: float = 3.0
    safety_margin: float = 1.5
    min_timeout_seconds: float = 5.0
    max_timeout_seconds: float = 120.0
    cache_suspected_min_input_tokens: int = 5000
    cache_suspected_max_ratio: float = 0.25
    cache_suspected_max_latency_seconds: float = 2.0


# LLM: FirstTokenTimeoutContext keeps probe-shape requirements out of core options.
# 类用途: 描述用于线性推算的 probe token 档位，默认使用 5K 和 10K。
@dataclass(frozen=True)
class FirstTokenTimeoutContext:
    required_probe_tokens: tuple[int, int] = (5000, 10000)


# LLM: FirstTokenTimeoutParams bundles all inputs for one first-token timeout estimate.
# 类用途: 作为 estimate_first_token_timeout 的参数包，集中目标输入、账本、选项和上下文。
@dataclass(frozen=True)
class FirstTokenTimeoutParams:
    input_tokens: int
    ledger: ModelCallLedger
    options: FirstTokenTimeoutOptions = FirstTokenTimeoutOptions()
    context: FirstTokenTimeoutContext = FirstTokenTimeoutContext()


# LLM: FirstTokenTimeoutEstimate is a structured timeout budget with provenance.
# 类用途: 返回首 token 超时、prefill 秒数、固定首 token 秒数和估算来源。
@dataclass(frozen=True)
class FirstTokenTimeoutEstimate:
    timeout_seconds: float
    prefill_seconds: float
    first_token_seconds: float
    source: str
    cache_suspected: bool = False

    # LLM: to_dict gives callers a primitive payload for ledgers or diagnostics.
    # 函数用途: 把估算结果转成普通 dict，字段名稳定供测试和日志读取。
    def to_dict(self) -> dict[str, float | str | bool]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "prefill_seconds": self.prefill_seconds,
            "first_token_seconds": self.first_token_seconds,
            "source": self.source,
            "cache_suspected": self.cache_suspected,
        }


# LLM: estimate_first_token_timeout uses probe facts when available and fallback math otherwise.
# 函数用途: 根据结构化 token 数和首 token 延迟样本计算动态首 token 超时预算。
def estimate_first_token_timeout(params: FirstTokenTimeoutParams) -> FirstTokenTimeoutEstimate:
    input_tokens = max(0, int(params.input_tokens))
    probe_estimate = _estimate_from_required_probes(
        input_tokens=input_tokens,
        ledger=params.ledger,
        options=params.options,
        context=params.context,
    )
    if probe_estimate is not None:
        return probe_estimate
    return _estimate_from_fallback(input_tokens=input_tokens, options=params.options)


# LLM: is_cache_suspected classifies unusually fast large-prompt first-token observations.
# 函数用途: 根据输入 token、实测首 token 延迟和估算预算返回缓存疑似布尔值。
def is_cache_suspected(
    *,
    input_tokens: int,
    first_token_latency_seconds: float,
    estimate: FirstTokenTimeoutEstimate,
    options: FirstTokenTimeoutOptions | None = None,
) -> bool:
    options = options or FirstTokenTimeoutOptions()
    if input_tokens < options.cache_suspected_min_input_tokens:
        return False
    observed = max(0.0, float(first_token_latency_seconds))
    expected = max(0.0, estimate.prefill_seconds + estimate.first_token_seconds)
    if observed <= max(0.0, options.cache_suspected_max_latency_seconds):
        return True
    return expected > 0 and observed <= expected * max(0.0, options.cache_suspected_max_ratio)


# LLM: _estimate_from_required_probes derives a line from configured probe token buckets.
# 函数用途: 查找 5K/10K 等 probe 样本，并用两点斜率估算目标输入的 prefill 成本。
def _estimate_from_required_probes(
    *,
    input_tokens: int,
    ledger: ModelCallLedger,
    options: FirstTokenTimeoutOptions,
    context: FirstTokenTimeoutContext,
) -> FirstTokenTimeoutEstimate | None:
    low_tokens, high_tokens = _ordered_probe_tokens(context.required_probe_tokens)
    low_sample = _latest_probe_sample(ledger.records(), low_tokens)
    high_sample = _latest_probe_sample(ledger.records(), high_tokens)
    if low_sample is None or high_sample is None:
        return None

    low_latency = float(low_sample.first_token_latency_seconds or 0.0)
    high_latency = float(high_sample.first_token_latency_seconds or 0.0)
    slope = max(0.0, (high_latency - low_latency) / max(1, high_tokens - low_tokens))
    first_token_seconds = max(0.0, low_latency - (slope * low_tokens))
    prefill_seconds = slope * input_tokens
    timeout_seconds = _clamp_timeout(
        (prefill_seconds + first_token_seconds) * _positive_or_default(options.safety_margin, 1.0),
        options,
    )
    return FirstTokenTimeoutEstimate(
        timeout_seconds=timeout_seconds,
        prefill_seconds=prefill_seconds,
        first_token_seconds=first_token_seconds,
        source=f"probe_{low_tokens // 1000}k_{high_tokens // 1000}k",
    )


# LLM: _estimate_from_fallback computes timeout from configured prefill throughput.
# 函数用途: 无 probe 样本时按 input_tokens/rate 加固定首 token 时间估算预算。
def _estimate_from_fallback(
    *,
    input_tokens: int,
    options: FirstTokenTimeoutOptions,
) -> FirstTokenTimeoutEstimate:
    rate = _positive_or_default(options.fallback_prefill_tokens_per_second, 1.0)
    prefill_seconds = input_tokens / rate
    first_token_seconds = max(0.0, float(options.base_first_token_seconds))
    timeout_seconds = _clamp_timeout(
        (prefill_seconds + first_token_seconds) * _positive_or_default(options.safety_margin, 1.0),
        options,
    )
    return FirstTokenTimeoutEstimate(
        timeout_seconds=timeout_seconds,
        prefill_seconds=prefill_seconds,
        first_token_seconds=first_token_seconds,
        source="fallback",
    )


# LLM: _latest_probe_sample selects the newest valid probe observation for a token bucket.
# 函数用途: 从账本记录中找指定 input_tokens 的 probe first_token 样本。
def _latest_probe_sample(
    records: tuple[ModelCallRecord, ...],
    input_tokens: int,
) -> ModelCallRecord | None:
    for record in reversed(records):
        if (
            record.is_probe
            and record.input_tokens == input_tokens
            and record.first_token_latency_seconds is not None
            and record.status in {"first_token", "finished"}
            and not record.cache_suspected
        ):
            return record
    return None


# LLM: _ordered_probe_tokens normalizes configured probe buckets before interpolation.
# 函数用途: 返回从小到大的两个 probe token 档位。
def _ordered_probe_tokens(probe_tokens: tuple[int, int]) -> tuple[int, int]:
    first, second = probe_tokens
    low_tokens = max(0, int(min(first, second)))
    high_tokens = max(low_tokens + 1, int(max(first, second)))
    return low_tokens, high_tokens


# LLM: _clamp_timeout applies configured lower and upper bounds to a timeout value.
# 函数用途: 将估算秒数限制在 min_timeout_seconds 和 max_timeout_seconds 之间。
def _clamp_timeout(value: float, options: FirstTokenTimeoutOptions) -> float:
    minimum = max(0.0, float(options.min_timeout_seconds))
    maximum = max(minimum, float(options.max_timeout_seconds))
    return max(minimum, min(maximum, float(value)))


# LLM: _positive_or_default protects calculations from zero, negative, or invalid numeric options.
# 函数用途: 将配置值规整为正数；无效时使用调用方默认值。
def _positive_or_default(value: float, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return numeric if numeric > 0 else default


__all__ = [
    "FirstTokenTimeoutContext",
    "FirstTokenTimeoutEstimate",
    "FirstTokenTimeoutOptions",
    "FirstTokenTimeoutParams",
    "estimate_first_token_timeout",
    "is_cache_suspected",
]
