"""Typed provider errors shared by CLI, runners, and recovery code."""

from __future__ import annotations


class ProviderRecoverableError(RuntimeError):
    """Base class for model-provider failures that can be retried or resumed."""


class ProviderTimeoutError(ProviderRecoverableError):
    """The provider did not return before the configured request timeout."""


class ProviderTransientError(ProviderRecoverableError):
    """The provider returned a temporary overload/rate-limit/disconnect error."""


class ProviderResponseError(ProviderRecoverableError):
    """The provider responded, but the payload did not match the expected schema."""

    def __init__(self, message: str, *, error_code: str = "", details: object | None = None):
        super().__init__(message)
        self.error_code = str(error_code or "").strip()
        self.details = details


class ProviderContextWindowError(ProviderResponseError):
    """The provider rejected the request because the input exceeded its context window."""

    def __init__(self, message: str, *, details: object | None = None):
        super().__init__(message, error_code="MODEL_CONTEXT_WINDOW_EXCEEDED", details=details)


def is_provider_recoverable_error(exc: BaseException) -> bool:
    """Return True for typed provider failures that should not be treated as task bugs."""
    return isinstance(exc, ProviderRecoverableError)


def is_provider_timeout_error(exc: BaseException) -> bool:
    """Return True when the model-provider request timed out."""
    return isinstance(exc, ProviderTimeoutError)


def is_provider_transient_error(exc: BaseException) -> bool:
    """Return True when the model-provider failure is temporary or rate-limited."""
    return isinstance(exc, ProviderTransientError)


def is_empty_provider_response_error(exc: BaseException) -> bool:
    """Return True when the provider returned a syntactically valid but empty model message."""
    return isinstance(exc, ProviderResponseError) and exc.error_code == "MODEL_EMPTY_RESPONSE"


def is_provider_context_window_error(exc: BaseException) -> bool:
    """Return True for typed provider context-window failures."""
    return isinstance(exc, ProviderContextWindowError)


def provider_recoverable_report(exc: BaseException, *, timeout_seconds: object = "") -> str:
    """Render the canonical operator-facing report for recoverable provider failures."""
    if is_provider_timeout_error(exc):
        return provider_timeout_report(exc, timeout_seconds=timeout_seconds)
    if is_provider_transient_error(exc):
        return provider_transient_report(exc)
    if isinstance(exc, ProviderResponseError):
        return provider_response_report(exc)
    return (
        "[provider_recoverable]\n"
        "模型接口出现可恢复异常，本次 run 已停止当前请求。\n"
        f"error={exc}\n"
        "建议下一步：查看已写入的任务状态和产物，然后从未完成部分继续。"
    )


def provider_timeout_report(exc: BaseException, *, timeout_seconds: object = "") -> str:
    """Render a compact timeout handoff for CLI and parent-agent recovery."""
    timeout = _timeout_text(timeout_seconds)
    return (
        "[provider_timeout]\n"
        f"模型接口请求超时{timeout}，本次 run 已停止等待。\n"
        f"error={exc}\n"
        "建议下一步：先查看已写入的 subagent/task 状态和 artifacts；"
        "如果需要恢复本次单轮 run，使用 memory-resume 或针对对应 task/run 做恢复。"
    )


def provider_transient_report(exc: BaseException) -> str:
    """Render a compact transient-provider handoff for CLI and parent recovery."""
    return (
        "[provider_transient]\n"
        "模型接口临时不可用或被限流，本次 run 已停止当前请求。\n"
        f"error={exc}\n"
        "建议下一步：稍后重试，或先查看已写入的子代理状态、产物和 memory archive；"
        "已完成的工作不要重跑，继续未完成部分即可。"
    )


def provider_response_report(exc: BaseException) -> str:
    """Render a malformed-provider-response handoff without exposing huge payloads."""
    return (
        "[provider_response_error]\n"
        "模型接口返回了无法按当前适配器解析的响应，本次 run 已停止当前请求。\n"
        f"error={exc}\n"
        "建议下一步：保留已完成工作，稍后重试或切换模型/适配器；不要把它当成任务本身失败。"
    )


def _timeout_text(timeout_seconds: object) -> str:
    """Format optional timeout metadata without forcing every caller to pass it."""
    text = str(timeout_seconds or "").strip()
    return f"（request_timeout={text}s）" if text else ""
