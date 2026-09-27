# LLM: 上下文窗口的唯一解析入口：填了 model_context_window_tokens 就用它，留空时依次看服务商元数据、后端配置窗口，
#   最后兜底 128000。只读，不探测网络；压力预检与 Compact 都必须用这里的结果。
# 模块用途: 算出当前模型真正按多大的上下文窗口来预检和压缩。
from __future__ import annotations

from collections.abc import Mapping

from ...settings.defaults import (
    DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS,
    configured_context_window_tokens,
)


# LLM: model_context_window_tokens 填了正整数就是用户显式容量，优先于服务商元数据；留空时先用服务商元数据，
#   再用后端自带的配置窗口，最后兜底 DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS（128000）。不额外探测。
# 函数用途: 给真实模型压力与 Compact 计算同一窗口，避免菜单配置只影响显示。
def resolve_model_context_window_tokens(agent: object) -> int:
    backend = getattr(agent, "backend", None)
    explicit = configured_context_window_tokens(getattr(agent, "config", None))
    if explicit > 0:
        return explicit
    provider_window = _provider_context_window(backend)
    if provider_window > 0:
        return provider_window
    configured = _positive_int(getattr(backend, "configured_context_window_tokens", 0))
    return configured if configured > 0 else DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS


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
