"""Typed provider errors shared by CLI, runners, and recovery code."""

from __future__ import annotations

from ..contracts.model_call_ledger import TIMEOUT_STAGES


class ProviderRecoverableError(RuntimeError):
    """Base class for model-provider failures that can be retried or resumed."""


# LLM: NET-01(2026-08-15 C3 真机): 连接失败(Connection refused/DNS/配置错)是不可重试的
# 配置性失败, 不能并入 ProviderRecoverableError(否则重试层会误重试); 单独成类让
# CLI/Gateway 能给出稳定用户提示而不打印 traceback。参照 长期助手 error_classifier 的
# connection 分类。
# 类用途: 模型接口无法连接(服务未起/DNS/代理/配置错误)的 typed 错误。
class ProviderConnectionError(RuntimeError):
    """Model endpoint unreachable (connection refused/DNS/config), not retryable."""


class ProviderTimeoutError(ProviderRecoverableError):
    """The provider did not return before the configured request timeout.

    ``stage`` 区分超时来源（门槛2）：``stream_idle``（SSE 数据流空闲掐断）、
    ``wall_clock``（本进程墙钟守卫线程超时）、``provider_declared``（provider
    网络层声明的 connect/read 超时）。旧调用不传时保持 ``provider_wall``
    （历史语义兼容读取，读取端按 wall_clock 族处理）。
    """

    # 门槛2 终审补证(seq1613b): stage 合同封闭——合法值集合三值 + legacy
    # provider_wall; 未知值构造即抛 ValueError(fail-closed), 防新路径写入
    # 未登记 stage 污染账本语义。
    # 终审边界②(seq1622-2): 集合引用账本层单一事实源 TIMEOUT_STAGES, 异常
    # 生产入口与账本写入入口共用同一合同, 不再各自维护。
    _KNOWN_STAGES = TIMEOUT_STAGES

    def __init__(self, message: str, *, stage: str = "provider_wall"):
        if stage not in self._KNOWN_STAGES:
            raise ValueError(
                f"未知 ProviderTimeoutError stage: {stage!r} "
                f"(合法: stream_idle/wall_clock/provider_declared/provider_wall)"
            )
        super().__init__(message)
        self.stage = stage


class ProviderTransientError(ProviderRecoverableError):
    """The provider returned a temporary overload/rate-limit/disconnect error."""


class ProviderUsageLimitError(ProviderTransientError):
    """The provider reported a structured usage/rate limit for this turn."""


class ProviderQuotaExhaustedError(ProviderRecoverableError):
    """The configured provider account has no usable quota for immediate retry."""

    def __init__(self, message: str, *, details: object | None = None):
        super().__init__(message)
        self.error_code = "PROVIDER_QUOTA_EXHAUSTED"
        self.details = details


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


def is_provider_usage_limit_error(exc: BaseException) -> bool:
    """Return True only for a provider HTTP usage/rate limit, not generic outages."""
    return isinstance(exc, (ProviderUsageLimitError, ProviderQuotaExhaustedError))


def is_provider_quota_exhausted_error(exc: BaseException) -> bool:
    """Return True when retrying the same credential cannot restore provider quota."""
    return isinstance(exc, ProviderQuotaExhaustedError)


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
    if is_provider_quota_exhausted_error(exc):
        return provider_quota_exhausted_report(exc)
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


def provider_quota_exhausted_report(exc: BaseException) -> str:
    """Render a fail-fast handoff for account/plan quota exhaustion."""
    return (
        "[provider_quota_exhausted]\n"
        "当前模型账号或套餐的可用额度已经耗尽，原地重试不会恢复。\n"
        f"error={exc}\n"
        "建议下一步：补充额度、等待套餐重置，或显式切换到已有权限的模型后继续。"
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
