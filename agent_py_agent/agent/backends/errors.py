# LLM: 模型异常类型和合法 HTTP 属性是分类/恢复的事实源；安全展示不能从异常正文推断状态码或配置错误。
# 模块用途: 统一 CLI、主子执行和恢复的模型错误边界，并投影可公开的 HTTP 数值。

from __future__ import annotations

from ..contracts.model_call_ledger import TIMEOUT_STAGES


class ProviderRecoverableError(RuntimeError):
    """Base class for model-provider failures that can be retried or resumed."""


# LLM: Permanent endpoint/model/credential mismatches share one typed provider boundary; callers may render them as configuration failures but must never retry or reinterpret them as model capability evidence.
# 类用途: 归类需要修改模型服务配置才能恢复的问题，避免被当成程序错误或瞬时断线反复重试。
class ProviderConfigurationError(RuntimeError):
    """Base class for permanent provider configuration failures."""

    error_code = "PROVIDER_CONFIGURATION_INVALID"


# LLM: ProviderConnectionError 只承载尚未证明可瞬时恢复的 DNS/地址/代理配置失败；typed ECONNREFUSED 已在 transport 边界归 ProviderTransientError。
# 类用途: 表示需要用户检查模型接口地址、DNS 或代理配置的不可重试连接错误。
class ProviderConnectionError(ProviderConfigurationError):
    """Model endpoint is unreachable because endpoint/DNS/proxy configuration is invalid."""

    error_code = "PROVIDER_CONNECTION_FAILED"


# LLM: 4xx 请求拒绝绕过能力 fallback 和原请求盲重试，但不证明密钥错误；保留 status/details 供诊断。
# 类用途: 表示服务拒绝了这次请求，可能是消息协议或参数问题，不把先前已成功执行的工作算成未执行。
class ProviderRequestRejectedError(ProviderConfigurationError):
    """The provider permanently rejected the configured request."""

    error_code = "PROVIDER_REQUEST_REJECTED"

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        details: object | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = max(0, int(status_code or 0))
        self.details = details


# LLM: 只接受异常上的真实整数 HTTP 属性，不解析正文、details 或布尔值；供诊断和分类共同使用。
# 函数用途: 返回可安全显示的 HTTP 状态码，缺失或非法时明确返回 None。
def provider_error_http_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if type(status) is int and 100 <= status <= 599 else None


# LLM: Provider timeout stage is a closed machine contract shared with the ledger; user-facing text never selects recovery behavior.
# 类用途: 表示模型请求在哪个结构化等待阶段超时，供退避、恢复和诊断统一使用。
class ProviderTimeoutError(ProviderRecoverableError):
    """The provider did not return before the configured request timeout.

    ``stage`` 区分超时来源：``first_event``（响应头/首个 SSE data 等待超时）、
    ``stream_idle``（首包后的 SSE 数据流空闲掐断）、
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
                f"(合法: first_event/stream_idle/wall_clock/provider_declared/provider_wall)"
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


# LLM: Configuration failures are typed provider facts but intentionally excluded from recoverable retries.
# 函数用途: 判断错误是否必须先修改接口、模型或密钥配置才能继续。
def is_provider_configuration_error(exc: BaseException) -> bool:
    return isinstance(exc, ProviderConfigurationError)


def is_provider_timeout_error(exc: BaseException) -> bool:
    """Return True when the model-provider request timed out."""
    return isinstance(exc, ProviderTimeoutError)


# LLM: Only transport-owned streaming stages may replay one sampling request through the
# 会话运行时 reconnect loop; total wall-clock and legacy/provider-declared timeouts retain
# their separate bounded retry contract.
# 函数用途: 判断一次超时是不是流式连接中断，可安全重发同一模型采样请求。
def is_provider_stream_timeout_error(exc: BaseException) -> bool:
    """Return True for first-event or between-event streaming timeouts."""
    return isinstance(exc, ProviderTimeoutError) and exc.stage in {
        "first_event",
        "stream_idle",
    }


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


# LLM: CLI 按异常类型区分请求拒绝与配置错误；此入口不知道此前工具是否运行，不能作零执行断言。
# 函数用途: 输出本次失败的性质和已有诊断，不误导用户修改密钥或重做已完成工作。
def provider_configuration_report(exc: BaseException) -> str:
    if isinstance(exc, ProviderRequestRejectedError):
        return (
            "[provider_request_rejected]\n"
            "模型服务拒绝了本次请求，本轮已停止；此前已执行的工作不会因此撤销。\n"
            f"error={exc}\n"
            "请查看请求诊断确定原因，不要直接重做整个任务。"
        )
    return (
        "[provider_configuration]\n"
        "本次模型请求无法使用当前配置，本轮已停止；此前已执行的工作不会因此撤销。\n"
        f"error={exc}\n"
        "请检查接口地址、模型名称和密钥是否属于同一服务后重试。"
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
