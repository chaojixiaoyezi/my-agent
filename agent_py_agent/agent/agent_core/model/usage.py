
from __future__ import annotations

from collections.abc import Mapping


def response_usage(response: object) -> dict[str, object]:
    usage = getattr(response, "usage", {})
    return dict(usage) if isinstance(usage, Mapping) else {}


def input_token_usage(response: object) -> int | None:
    usage = response_usage(response)
    return _first_positive_int(
        (
            usage.get("input_tokens"),
            usage.get("prompt_tokens"),
            usage.get("cache_creation_input_tokens"),
        )
    )


# LLM: Provider-visible context and billing input are not always the same field shape. Anthropic
# reports uncached input, cache reads, and cache writes as three disjoint top-level counters, while
# OpenAI-style prompt/input totals already include the nested cached-token detail. Never add nested
# detail to an inclusive total or use this helper for cost subtraction.
# 函数用途: 把不同厂商的用量字段还原成“本次模型实际看到的输入 token 总数”。
def provider_visible_input_token_usage(response: object) -> int | None:
    usage = response_usage(response)
    prompt_total = _first_positive_int((usage.get("prompt_tokens"),))
    if prompt_total is not None:
        return prompt_total

    input_total = _first_positive_int((usage.get("input_tokens"),))
    separate_cache_read = _first_positive_int(
        (usage.get("cache_read_input_tokens"),)
    )
    separate_cache_write = _first_positive_int(
        (
            usage.get("cache_creation_input_tokens"),
            usage.get("cache_write_input_tokens"),
        )
    )
    if input_total is not None:
        return input_total + (separate_cache_read or 0) + (separate_cache_write or 0)
    if separate_cache_read is None and separate_cache_write is None:
        return None
    return (separate_cache_read or 0) + (separate_cache_write or 0)


def output_token_usage(response: object) -> int | None:
    usage = response_usage(response)
    return _first_positive_int((usage.get("output_tokens"), usage.get("completion_tokens")))


def cached_input_token_usage(response: object) -> int:
    """Return provider-reported cached input that is included in input/prompt totals."""
    usage = response_usage(response)
    for key in ("input_tokens_details", "prompt_tokens_details"):
        details = usage.get(key)
        if isinstance(details, Mapping):
            value = _first_positive_int((details.get("cached_tokens"),))
            if value is not None:
                return value
    # Anthropic's cache_read_input_tokens is separate from input_tokens, so it
    # must not be subtracted from that already non-cached input count.
    return 0


# LLM: Cost/accounting subtraction and observability are different contracts. This helper reports
# every provider cache-read field without assuming whether it is already included in input_tokens.
# 函数用途: 读取供应商实际返回的缓存命中 token，供任务消耗对比单独展示。
def reported_cache_read_token_usage(response: object) -> int:
    usage = response_usage(response)
    for key in ("input_tokens_details", "prompt_tokens_details"):
        details = usage.get(key)
        if isinstance(details, Mapping):
            value = _first_positive_int(
                (details.get("cached_tokens"), details.get("cache_read_tokens"))
            )
            if value is not None:
                return value
    return _first_positive_int(
        (usage.get("cache_read_input_tokens"), usage.get("cached_input_tokens"))
    ) or 0


# LLM: Cache creation is reported separately by Anthropic-style providers and must not be merged
# into cache reads or guessed from latency.
# 函数用途: 读取本次请求新写入供应商缓存的 token 数。
def cache_creation_input_token_usage(response: object) -> int:
    usage = response_usage(response)
    for key in ("input_tokens_details", "prompt_tokens_details"):
        details = usage.get(key)
        if isinstance(details, Mapping):
            value = _first_positive_int(
                (
                    details.get("cache_creation_tokens"),
                    details.get("cache_write_tokens"),
                )
            )
            if value is not None:
                return value
    return _first_positive_int(
        (
            usage.get("cache_creation_input_tokens"),
            usage.get("cache_write_input_tokens"),
        )
    ) or 0


def goal_token_usage(response: object) -> int:
    """Match 会话运行时 goal accounting: non-cached input plus output tokens."""
    input_tokens = input_token_usage(response) or 0
    output_tokens = output_token_usage(response) or 0
    return max(0, input_tokens - cached_input_token_usage(response)) + output_tokens


def _first_positive_int(values: tuple[object, ...]) -> int | None:
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def response_cost_usd(model: str, response: object) -> float:
    """按模型单价把一次响应的 input/output token 换算成 USD 成本(审计 #19)。

    token 缺失按 0;单价表见 llm_scale.model_pricing(未知模型保守默认,不低估)。供成本审计/计费累加。
    """
    from ...llm_scale.model_pricing import cost_usd

    return cost_usd(model, input_token_usage(response) or 0, output_token_usage(response) or 0)


__all__ = [
    "cache_creation_input_token_usage",
    "cached_input_token_usage",
    "goal_token_usage",
    "input_token_usage",
    "output_token_usage",
    "provider_visible_input_token_usage",
    "reported_cache_read_token_usage",
    "response_cost_usd",
    "response_usage",
]
