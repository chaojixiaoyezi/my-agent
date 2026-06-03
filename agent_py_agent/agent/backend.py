
from __future__ import annotations

"""compatibility facade for model backend adapters moved to `agent.backends`.

真实后端实现已经放到 `agent_py_agent.agent.backends`。
这个文件只保留旧导入路径，避免历史代码里的 `from agent.backend import ...` 失效。
"""

from .backends import (  # noqa: F401
    AnthropicCompatibleBackend,
    BackendOptions,
    BaseBackend,
    EchoBackend,
    HttpBackend,
    ModelResponse,
    OpenAICompatibleBackend,
    ProviderRecoverableError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderTransientError,
    get_backend,
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
    "ProviderRecoverableError",
    "ProviderResponseError",
    "ProviderTimeoutError",
    "ProviderTransientError",
    "get_backend",
    "is_provider_recoverable_error",
    "is_provider_timeout_error",
    "is_provider_transient_error",
    "provider_recoverable_report",
    "provider_response_report",
    "provider_timeout_report",
    "provider_transient_report",
]
