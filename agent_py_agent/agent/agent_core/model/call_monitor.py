from __future__ import annotations

from dataclasses import dataclass

from ...contracts.model_call_ledger import ModelCallLedger, ModelCallRecord


# LLM: First-event estimation parameters are pure request-planning inputs; they never mutate backend transport state.
# 类用途: 保存首 token 预算所需的吞吐、上下限与 probe 采样策略。
@dataclass(frozen=True)
class FirstTokenTimeoutOptions:
    estimated_prefill_tokens_per_second: float = 200.0
    base_first_token_seconds: float = 3.0
    safety_margin: float = 1.5
    min_timeout_seconds: float = 5.0
    max_timeout_seconds: float = 120.0
    cache_suspected_min_input_tokens: int = 5000
    cache_suspected_max_ratio: float = 0.25
    cache_suspected_max_latency_seconds: float = 2.0
    # 门槛4: probe 统计参数——最小样本数/滑窗上限/异常值去极值开关。
    # 每个 probe token 点样本不足 min_samples 时回退 fixed-rate 估计;
    # 窗口最多取最近 window_samples 条; trim 开启时窗口>=3 去掉极值取均值。
    probe_min_samples: int = 2
    probe_window_samples: int = 5
    probe_outlier_trim: bool = True


@dataclass(frozen=True)
class FirstTokenTimeoutContext:
    required_probe_tokens: tuple[int, int] = (5000, 10000)


@dataclass(frozen=True)
class FirstTokenTimeoutParams:
    input_tokens: int
    ledger: ModelCallLedger
    options: FirstTokenTimeoutOptions = FirstTokenTimeoutOptions()
    context: FirstTokenTimeoutContext = FirstTokenTimeoutContext()


@dataclass(frozen=True)
class FirstTokenTimeoutEstimate:
    timeout_seconds: float
    prefill_seconds: float
    first_token_seconds: float
    source: str
    cache_suspected: bool = False

    def to_dict(self) -> dict[str, float | str | bool]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "prefill_seconds": self.prefill_seconds,
            "first_token_seconds": self.first_token_seconds,
            "source": self.source,
            "cache_suspected": self.cache_suspected,
        }


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
    return _estimate_from_estimated_rate(input_tokens=input_tokens, options=params.options)


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


def _estimate_from_required_probes(
    *,
    input_tokens: int,
    ledger: ModelCallLedger,
    options: FirstTokenTimeoutOptions,
    context: FirstTokenTimeoutContext,
) -> FirstTokenTimeoutEstimate | None:
    low_tokens, high_tokens = _ordered_probe_tokens(context.required_probe_tokens)
    # 门槛4: 滑窗内取样本(每点最多最近 window_samples 条), 任一点不足
    # min_samples 即回退 fixed-rate(最小样本数防线, 防单样本噪声主导)。
    low_records = _probe_window_records(
        ledger.records(),
        low_tokens,
        int(options.probe_window_samples),
    )
    high_records = _probe_window_records(
        ledger.records(),
        high_tokens,
        int(options.probe_window_samples),
    )
    min_samples = max(1, int(options.probe_min_samples))
    if len(low_records) < min_samples or len(high_records) < min_samples:
        return None

    low_latency = _window_latency_seconds(low_records, bool(options.probe_outlier_trim))
    high_latency = _window_latency_seconds(high_records, bool(options.probe_outlier_trim))
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
        source=(
            f"probe_{low_tokens // 1000}k_{high_tokens // 1000}k_"
            f"n{min(len(low_records), len(high_records))}"
        ),
    )


def _estimate_from_estimated_rate(
    *,
    input_tokens: int,
    options: FirstTokenTimeoutOptions,
) -> FirstTokenTimeoutEstimate:
    rate = _positive_or_default(options.estimated_prefill_tokens_per_second, 1.0)
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
        source="estimated_rate",
    )


def _probe_window_records(
    records: tuple[ModelCallRecord, ...],
    input_tokens: int,
    window: int,
) -> list[ModelCallRecord]:
    """门槛4: 某 probe token 点的最近 window 条有效样本(时间倒序取窗口)。

    有效样本: is_probe + input_tokens 精确匹配 + 有 first_token_latency +
    first_token/finished + 非 cache_suspected(与旧 _latest_probe_sample 同判据,
    从单条扩展为滑窗)。
    """
    matched = [
        record
        for record in reversed(records)
        if (
            record.is_probe
            and record.input_tokens == input_tokens
            and record.first_token_latency_seconds is not None
            and record.status in {"first_token", "finished"}
            and not record.cache_suspected
        )
    ]
    return matched[: max(1, int(window))]


def _window_latency_seconds(records: list[ModelCallRecord], trim: bool) -> float:
    """窗口样本的时延估计: trim 开启且样本>=3 时去掉最小最大后取均值
    (异常值去极值); trim 关闭或样本<3 时全样本均值。"""
    values = sorted(float(record.first_token_latency_seconds or 0.0) for record in records)
    if trim and len(values) >= 3:
        values = values[1:-1]
    return sum(values) / max(1, len(values))


def _ordered_probe_tokens(probe_tokens: tuple[int, int]) -> tuple[int, int]:
    first, second = probe_tokens
    low_tokens = max(0, int(min(first, second)))
    high_tokens = max(low_tokens + 1, int(max(first, second)))
    return low_tokens, high_tokens


def _clamp_timeout(value: float, options: FirstTokenTimeoutOptions) -> float:
    minimum = max(0.0, float(options.min_timeout_seconds))
    maximum = max(minimum, float(options.max_timeout_seconds))
    return max(minimum, min(maximum, float(value)))


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
