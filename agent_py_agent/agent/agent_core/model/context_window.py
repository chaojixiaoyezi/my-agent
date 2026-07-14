
from __future__ import annotations

from collections.abc import Mapping

DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000


def resolve_model_context_window_tokens(agent: object) -> int:
    backend = getattr(agent, "backend", None)
    provider_window = _provider_context_window(backend)
    if provider_window > 0:
        return provider_window
    configured = _configured_context_window(agent, backend)
    return configured if configured > 0 else DEFAULT_CONTEXT_WINDOW_TOKENS


def _provider_context_window(backend: object | None) -> int:
    if backend is None:
        return 0
    direct_values = [
        _metadata_value(getattr(backend, "provider_context_window_tokens", 0)),
        getattr(backend, "max_context_tokens", 0),
        getattr(backend, "context_length", 0),
        getattr(backend, "model_context_window_tokens", 0),
    ]
    # 外部/测试 backend 的 context_window_tokens 仍视为其自报事实；内置 HTTP backend
    # 同时带 configured_context_window_tokens，因此兼容属性不能混入 provider 裁决。
    if not hasattr(backend, "configured_context_window_tokens"):
        direct_values.append(getattr(backend, "context_window_tokens", 0))
    direct = _first_positive(direct_values)
    return direct if direct > 0 else _window_from_backend_metadata(backend)


def _configured_context_window(agent: object, backend: object | None) -> int:
    config = getattr(agent, "config", None)
    configured = _positive_int(getattr(config, "model_context_window_tokens", 0))
    if configured > 0:
        return configured
    return _positive_int(getattr(backend, "configured_context_window_tokens", 0))


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
