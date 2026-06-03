
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


__all__ = ["input_token_usage", "output_token_usage", "response_usage"]
