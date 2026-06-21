
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


def output_token_usage(response: object) -> int | None:
    usage = response_usage(response)
    return _first_positive_int((usage.get("output_tokens"), usage.get("completion_tokens")))


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


__all__ = ["input_token_usage", "output_token_usage", "response_usage", "response_cost_usd"]
