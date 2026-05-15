# LLM: Model backend module; keep streaming, gateway, and backend protocol shapes stable.
# 模块用途: 封装模型后端协议、流式解析和 gateway 辅助调用。

from __future__ import annotations

"""public API for model backend adapters and normalized model responses.

这里放所有'怎么和模型服务说话'的代码。以后新增 OpenAI、Anthropic、本地模型、
公司内网模型，都应该进这个目录，而不是塞回 `core.py`。
"""

from .base import (
    AnthropicCompatibleBackend,
    BackendOptions,
    BaseBackend,
    EchoBackend,
    HttpBackend,
    ModelResponse,
    OpenAICompatibleBackend,
    get_backend,
)
from .errors import (
    ProviderTimeoutError,
    ProviderTransientError,
    is_provider_timeout_error,
    is_provider_transient_error,
    provider_timeout_report,
)

__all__ = [
    "AnthropicCompatibleBackend",
    "BaseBackend",
    "BackendOptions",
    "EchoBackend",
    "HttpBackend",
    "ModelResponse",
    "OpenAICompatibleBackend",
    "ProviderTimeoutError",
    "ProviderTransientError",
    "get_backend",
    "is_provider_timeout_error",
    "is_provider_transient_error",
    "provider_timeout_report",
]
