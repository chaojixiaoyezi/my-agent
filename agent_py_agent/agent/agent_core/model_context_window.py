# LLM: Context-window resolution trusts model/backend metadata first, then a generic fallback.
# 模块用途: 从当前模型后端暴露的元数据读取上下文窗口；拿不到就用 128K 通用兜底，不让用户维护模型窗口表。

from __future__ import annotations

from collections.abc import Mapping

DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000


# LLM: resolve_model_context_window_tokens is the single source for compact context-window budgets.
# 函数用途: 返回后端真实暴露的上下文窗口；未知时用 128K 兜底，让自动 compact 仍能提前工作。
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


# LLM: _window_from_backend_metadata checks known backend metadata containers without hardcoding model names.
# 函数用途: 从 backend 元数据字典读取上下文窗口；没有可信字段时返回 0。
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


# LLM: _metadata_value tolerates metadata exposed as either a property value or a zero-arg method.
# 函数用途: 安全读取 backend metadata；调用失败时当作未知窗口处理。
def _metadata_value(value: object) -> object:
    if not callable(value):
        return value
    try:
        return value()
    except TypeError:
        return None


# LLM: _first_positive picks the first valid positive integer from a known list of candidate values.
# 函数用途: 按优先级读取窗口候选值，避免把 0、空值或坏字符串当成可用窗口。
def _first_positive(values: list[object]) -> int:
    for value in values:
        parsed = _positive_int(value)
        if parsed > 0:
            return parsed
    return 0


# LLM: _positive_int is a strict positive-int parser for model metadata fields.
# 函数用途: 把元数据字段转成正整数；非法值或非正数统一返回 0。
def _positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


__all__ = ["resolve_model_context_window_tokens"]
