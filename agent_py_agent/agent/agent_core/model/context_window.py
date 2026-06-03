
from __future__ import annotations

from collections.abc import Mapping

DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000


def resolve_model_context_window_tokens(agent: object) -> int:
    backend = getattr(agent, "backend", None)
    if backend is None:
        return DEFAULT_CONTEXT_WINDOW_TOKENS
    direct = _first_positive([
        getattr(backend, "context_window_tokens", 0),
        getattr(backend, "max_context_tokens", 0),
        getattr(backend, "context_length", 0),
        getattr(backend, "model_context_window_tokens", 0),
    ])
    if direct > 0:
        return direct
    metadata_window = _window_from_backend_metadata(backend)
    return metadata_window if metadata_window > 0 else DEFAULT_CONTEXT_WINDOW_TOKENS


def _window_from_backend_metadata(backend: object) -> int:
    for attr in ("model_metadata", "metadata", "capabilities", "model_capabilities"):
        metadata = _metadata_value(getattr(backend, attr, None))
        if not isinstance(metadata, Mapping):
            continue
        window = _first_positive([
            metadata.get("context_window_tokens"),
            metadata.get("max_context_tokens"),
            metadata.get("context_length"),
            metadata.get("context_window"),
            metadata.get("input_token_limit"),
            metadata.get("max_input_tokens"),
        ])
        if window > 0:
            return window
    return 0


def _metadata_value(value: object) -> object:
    if not callable(value):
        return value
    try:
        return value()
    except TypeError:
        return None


def _first_positive(values: list[object]) -> int:
    for value in values:
        parsed = _positive_int(value)
        if parsed > 0:
            return parsed
    return 0


def _positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


__all__ = ["resolve_model_context_window_tokens"]
