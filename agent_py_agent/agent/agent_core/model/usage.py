
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


def response_usage(response: object) -> dict[str, object]:
    usage = getattr(response, "usage", {})
    return dict(usage) if isinstance(usage, Mapping) else {}


# LLM: 用量字段的协议形状必须按"字段组合"判定，不能按模型名猜：Anthropic 兼容把未缓存输入/
# 缓存读/缓存写放在三个互斥顶层字段；OpenAI 兼容的 prompt_tokens 已含缓存明细；
# OpenAI Responses 风格的 input_tokens 也含嵌套 input_tokens_details.cached_tokens。
# 归一后 total 才是"模型实际看到的输入"，账本/成本/Goal 都只能读它，不许各自再猜一遍协议。
# 类用途: 保存一次响应的归一用量，以及"是否存在缺字段"（partial 不得当成精确 0）。
@dataclass(frozen=True)
class NormalizedUsage:
    protocol: str
    input_tokens: int
    cached_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int
    partial: bool


# LLM: 归一入口是唯一的协议判定点；任何新增协议只在这里加分支，消费者不改。
# 函数用途: 把供应商原始 usage 还原成"总输入 + 缓存命中 + 缓存写入 + 输出"的统一结构。
def normalize_usage(response: object) -> NormalizedUsage:
    usage = response_usage(response)
    output_reported = output_token_usage(response)
    output = output_reported or 0
    partial = output_reported is None

    nested = None
    for key in ("input_tokens_details", "prompt_tokens_details"):
        value = usage.get(key)
        if isinstance(value, Mapping):
            nested = value
            break
    nested_cached = None
    if nested is not None:
        nested_cached = _first_positive_int(
            (nested.get("cached_tokens"), nested.get("cache_read_tokens"))
        )
    top_read = _first_positive_int(
        (usage.get("cache_read_input_tokens"), usage.get("cached_input_tokens"))
    )
    top_write = _first_positive_int(
        (usage.get("cache_creation_input_tokens"), usage.get("cache_write_input_tokens"))
    )

    prompt_total = _first_positive_int((usage.get("prompt_tokens"),))
    if prompt_total is not None:
        # OpenAI 兼容：prompt 总数已包含缓存明细，不能再加一次。
        return NormalizedUsage(
            "openai_compatible", prompt_total, nested_cached or 0, top_write or 0, output, partial
        )

    input_total = _first_positive_int((usage.get("input_tokens"),))
    if input_total is not None:
        if nested_cached is not None and top_read is None:
            # OpenAI Responses 风格：input_tokens 已含嵌套缓存明细。
            return NormalizedUsage(
                "openai_responses", input_total, nested_cached, top_write or 0, output, partial
            )
        read = top_read or 0
        write = top_write or 0
        if usage.get("cache_read_input_tokens") is None and top_write is None and read == 0 and write == 0:
            partial = True
        return NormalizedUsage(
            "anthropic_compatible", input_total + read + write, read, write, output, partial
        )

    if top_read is None and top_write is None:
        partial = True
    return NormalizedUsage("unknown", 0, top_read or 0, top_write or 0, output, partial)


# LLM: 累计处理量口径 = 协议归一后的总输入（含缓存读写，重复读取也累计）；这是账本
# accounted_input_tokens 的唯一来源，不能再用"取第一个非空字段"的旧启发式。
# 函数用途: 读取本次响应的总输入 token。
def input_token_usage(response: object) -> int | None:
    normalized = normalize_usage(response)
    if normalized.protocol == "unknown" and normalized.input_tokens == 0:
        return None
    return normalized.input_tokens


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
    """Match 会话运行时 goal accounting: non-cached input plus output tokens.

    使用"供应商可见总输入（已按协议归一）+ 输出，再减去缓存命中"的口径：
    Anthropic 兼容的总输入含 cache read + cache write，OpenAI 的 prompt_tokens 已含嵌套 cached；
    两者都只能减去 *缓存命中* 部分。旧实现减的是"嵌套 details 里的 cached"，对 Anthropic 恒为 0，
    于是把缓存命中当普通输入全部计入（实测 100 普通 + 200 写 + 700 读 + 50 出 时算成 150 而非 350）。
    """
    visible_input = provider_visible_input_token_usage(response)
    if visible_input is None:
        visible_input = input_token_usage(response) or 0
    cached_read = reported_cache_read_token_usage(response)
    output_tokens = output_token_usage(response) or 0
    return max(0, visible_input - cached_read) + output_tokens


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
