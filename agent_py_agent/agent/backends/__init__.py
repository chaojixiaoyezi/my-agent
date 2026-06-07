
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
    ProviderContextWindowError,
    ProviderRecoverableError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransientError,
    is_provider_context_window_error,
    is_provider_recoverable_error,
    is_provider_timeout_error,
    is_provider_transient_error,
    provider_recoverable_report,
    provider_response_report,
    provider_timeout_report,
    provider_transient_report,
)

__all__ = [
    "AnthropicCompatibleBackend",
    "BaseBackend",
    "BackendOptions",
    "EchoBackend",
    "HttpBackend",
    "ModelResponse",
    "OpenAICompatibleBackend",
    "ProviderContextWindowError",
    "ProviderRecoverableError",
    "ProviderResponseError",
    "ProviderTimeoutError",
    "ProviderTransientError",
    "get_backend",
    "is_provider_context_window_error",
    "is_provider_recoverable_error",
    "is_provider_timeout_error",
    "is_provider_transient_error",
    "provider_recoverable_report",
    "provider_response_report",
    "provider_timeout_report",
    "provider_transient_report",
]
