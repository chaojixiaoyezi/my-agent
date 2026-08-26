# LLM: 本包门面按符号延迟加载；读取 typed provider error 不能顺带构造 HTTP backend、SDK 或模型适配器。
# 模块用途: 保留模型后端公开导入路径，同时让 CLI、状态读取和错误分类保持轻量启动。

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "AnthropicCompatibleBackend": ("base", "AnthropicCompatibleBackend"),
    "BackendOptions": ("base", "BackendOptions"),
    "BaseBackend": ("base", "BaseBackend"),
    "EchoBackend": ("base", "EchoBackend"),
    "HttpBackend": ("base", "HttpBackend"),
    "ModelResponse": ("base", "ModelResponse"),
    "OpenAICompatibleBackend": ("base", "OpenAICompatibleBackend"),
    "ProviderRequestOptions": ("base", "ProviderRequestOptions"),
    "get_backend": ("base", "get_backend"),
    "ProviderContextWindowError": ("errors", "ProviderContextWindowError"),
    "ProviderQuotaExhaustedError": ("errors", "ProviderQuotaExhaustedError"),
    "ProviderConnectionError": ("errors", "ProviderConnectionError"),
    "ProviderConfigurationError": ("errors", "ProviderConfigurationError"),
    "ProviderRecoverableError": ("errors", "ProviderRecoverableError"),
    "ProviderRequestRejectedError": ("errors", "ProviderRequestRejectedError"),
    "ProviderResponseError": ("errors", "ProviderResponseError"),
    "ProviderTimeoutError": ("errors", "ProviderTimeoutError"),
    "ProviderTransientError": ("errors", "ProviderTransientError"),
    "ProviderUsageLimitError": ("errors", "ProviderUsageLimitError"),
    "is_provider_context_window_error": ("errors", "is_provider_context_window_error"),
    "is_provider_configuration_error": ("errors", "is_provider_configuration_error"),
    "is_provider_quota_exhausted_error": ("errors", "is_provider_quota_exhausted_error"),
    "is_provider_recoverable_error": ("errors", "is_provider_recoverable_error"),
    "is_provider_timeout_error": ("errors", "is_provider_timeout_error"),
    "is_provider_transient_error": ("errors", "is_provider_transient_error"),
    "is_provider_usage_limit_error": ("errors", "is_provider_usage_limit_error"),
    "provider_quota_exhausted_report": ("errors", "provider_quota_exhausted_report"),
    "provider_configuration_report": ("errors", "provider_configuration_report"),
    "provider_recoverable_report": ("errors", "provider_recoverable_report"),
    "provider_response_report": ("errors", "provider_response_report"),
    "provider_timeout_report": ("errors", "provider_timeout_report"),
    "provider_transient_report": ("errors", "provider_transient_report"),
}

__all__ = list(_EXPORTS)


# LLM: 只解析声明的公开符号或真实兄弟模块，并把结果缓存到包对象；未知名称必须保持标准 AttributeError。
# 函数用途: 第一次真正使用某个后端能力时再导入对应实现。
def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is not None:
        module_name, attribute_name = target
        module = importlib.import_module(f".{module_name}", __name__)
        value = getattr(module, attribute_name)
        globals()[name] = value
        return value
    try:
        module = importlib.import_module(f".{name}", __name__)
    except ModuleNotFoundError as exc:
        if exc.name == f"{__name__}.{name}":
            raise AttributeError(name) from None
        raise
    globals()[name] = module
    return module


# LLM: 补全只列目录，不触发任何延迟导入。
# 函数用途: 让调试器和 IDE 能看到公开后端符号。
def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
