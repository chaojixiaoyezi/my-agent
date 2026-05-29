# LLM: Model usage helpers keep provider token metadata separate from local estimates.
# 模块用途: 读取模型返回的 usage 字段；没有真实 usage 时调用方继续使用本地估算。

from __future__ import annotations

from collections.abc import Mapping


# LLM: response_usage reads only structured usage payloads from ModelResponse-like objects.
# 函数用途: 返回模型真实 usage；字段不存在或不是对象时返回空字典。
def response_usage(response: object) -> dict[str, object]:
    usage = getattr(response, "usage", {})
    return dict(usage) if isinstance(usage, Mapping) else {}


# LLM: input_token_usage prefers provider input/prompt token fields over local prompt estimates.
# 函数用途: 从不同 provider usage 命名中读取输入 token；没有可信字段时返回 None。
def input_token_usage(response: object) -> int | None:
    usage = response_usage(response)
    return _first_positive_int(
        (
            usage.get("input_tokens"),
            usage.get("prompt_tokens"),
            usage.get("cache_creation_input_tokens"),
        )
    )


# LLM: output_token_usage prefers provider output/completion token fields over text estimates.
# 函数用途: 从不同 provider usage 命名中读取输出 token；没有可信字段时返回 None。
def output_token_usage(response: object) -> int | None:
    usage = response_usage(response)
    return _first_positive_int((usage.get("output_tokens"), usage.get("completion_tokens")))


# LLM: _first_positive_int accepts a fixed iterable to keep service interfaces explicit.
# 函数用途: 从 provider usage 候选字段里读取第一个非负整数。
def _first_positive_int(values: tuple[object, ...]) -> int | None:
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


__all__ = ["input_token_usage", "output_token_usage", "response_usage"]
