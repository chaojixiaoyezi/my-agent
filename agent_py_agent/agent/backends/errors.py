# LLM: 模型异常类型和合法 HTTP 属性是分类/恢复的事实源；安全展示不能从异常正文推断状态码或配置错误。
# 模块用途: 统一 CLI、主子执行和恢复的模型错误边界，并投影可公开的 HTTP 数值。

from __future__ import annotations

from ..contracts.model_call_ledger import TIMEOUT_STAGES, TIMEOUT_WAIT_PHASES


class ProviderRecoverableError(RuntimeError):
    """Base class for model-provider failures that can be retried or resumed."""


# LLM: Permanent endpoint/model/credential mismatches share one typed provider boundary; callers may render them as configuration failures but must never retry or reinterpret them as model capability evidence.
# 类用途: 归类需要修改模型服务配置才能恢复的问题，避免被当成程序错误或瞬时断线反复重试。
class ProviderConfigurationError(RuntimeError):
    """Base class for permanent provider configuration failures."""

    error_code = "PROVIDER_CONFIGURATION_INVALID"


# LLM: 本地配置缺失不是上游拒绝或瞬时故障；不得发请求、重试或切换其它模型。
# 类用途: 提示用户先配置模型，同时允许无模型状态下使用设置菜单和查看历史。
class ModelNotConfiguredError(ProviderConfigurationError):
    error_code = "MODEL_NOT_CONFIGURED"

    # LLM: CLI、Gateway 和后台共用同一异常身份；可选档案诊断只含编号与结构化原因，不带端点、密钥或输入正文。
    # 函数用途: 给未配置模型的调用返回明确、可操作的错误。
    def __init__(self, *, profile_id: str = "", profile_reason: str = "") -> None:
        super().__init__("尚未配置模型，请先通过 /model 新增并选择模型；系统不会自动使用其它模型。")
        self.profile_id = profile_id
        self.profile_reason = profile_reason


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


# LLM: 出口合同（backends.wire_contract）在发送前发现请求形状违反协议规则时抛出，请求没有发出、没有 HTTP 状态码。作为
#   ProviderRequestRejectedError 子类沿用"确定性拒绝、不盲重试、不当环境故障"的全部处理，只用错误码区分本地发现。
#   details 只含 protocol/rule/index 三个结构化字段，不含消息正文。
# 类用途: 让"发送前就查出消息结构不合规"成为可识别的本地错误，而不是打到服务端再换回一个 400。
class ProviderRequestShapeInvalidError(ProviderRequestRejectedError):
    error_code = "PROVIDER_REQUEST_SHAPE_INVALID"

    # LLM: 文案只供展示，分类与诊断读 error_code 和 details。
    # 函数用途: 记录违规的协议、规则名和消息位置。
    def __init__(self, *, protocol: str, rule: str, index: int) -> None:
        super().__init__(
            f"请求发出前的协议检查未通过：{protocol} 第 {index} 项违反 {rule}，请求没有发出。",
            details={"protocol": protocol, "rule": rule, "index": index},
        )


# LLM: 只接受异常上的真实整数 HTTP 属性，不解析正文、details 或布尔值；供诊断和分类共同使用。
# 函数用途: 返回可安全显示的 HTTP 状态码，缺失或非法时明确返回 None。
def provider_error_http_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if type(status) is int and 100 <= status <= 599 else None


# 环境级故障的 HTTP 状态：401 未授权、402 欠费、403 拒绝、404 端点或模型不存在、407 代理需要鉴权。
# 密钥过期、账户欠费、端点配错、代理鉴权失败都是环境坏了，不是某次请求有毒；400/413/422 这类请求本身的问题不在内。
_ENVIRONMENT_HTTP_STATUSES = frozenset({401, 402, 403, 404, 407})


# LLM: 环境级故障的唯一权威，唤醒毒丸（不计数）和 Gateway 后台车道（按车道暂停）共用；只读结构化整数状态码与异常类型，
#   不读正文。ModelNotConfiguredError 是 ProviderConfigurationError 的子类，这里也返回 True；需要"等模型配置"语义的
#   调用方必须先判 is_model_configuration_unavailable。改动联测 test_wake_poison 与 test_gateway_lane_retry。
# 函数用途: 判断一次模型错误是不是密钥、欠费、端点、代理或连接配置这类环境问题，而不是这次请求本身的问题。
def is_provider_environment_fault(exc: BaseException) -> bool:
    if provider_error_http_status(exc) in _ENVIRONMENT_HTTP_STATUSES:
        return True
    return isinstance(exc, ProviderConfigurationError) and not isinstance(exc, ProviderRequestRejectedError)


# LLM: Provider timeout stage is a closed machine contract shared with the ledger; user-facing text never selects recovery behavior.
# 类用途: 表示模型请求在哪个结构化等待阶段超时，供退避、恢复和诊断统一使用。
class ProviderTimeoutError(ProviderRecoverableError):
    """The provider did not return before the configured request timeout.

    ``stage`` 区分超时来源：``first_event``（响应头/首个 SSE data 等待超时）、
    ``stream_idle``（首包后的 SSE 数据流空闲掐断）、
    ``wall_clock``（本进程墙钟守卫线程超时）、``provider_declared``（provider
    网络层声明的 connect/read 超时）。旧调用不传时保持 ``provider_wall``
    （历史语义兼容读取，读取端按 wall_clock 族处理）。

    ``wait_phase`` 是只用于诊断的补充事实：同一个 ``first_event`` 可能来自真·首事件等待
    （连接已打开），也可能来自 WebSocket 握手（连接还没打开）。回归规则、退避集合和
    重试语义只看 ``stage``，绝不看 ``wait_phase``；它只进调用账本供事后统计区分两者。
    """

    # 门槛2 终审补证(seq1613b): stage 合同封闭——合法值集合三值 + legacy
    # provider_wall; 未知值构造即抛 ValueError(fail-closed), 防新路径写入
    # 未登记 stage 污染账本语义。
    # 终审边界②(seq1622-2): 集合引用账本层单一事实源 TIMEOUT_STAGES, 异常
    # 生产入口与账本写入入口共用同一合同, 不再各自维护。
    _KNOWN_STAGES = TIMEOUT_STAGES
    # 诊断等待位置同样封闭（引用账本层单一事实源 TIMEOUT_WAIT_PHASES）：空串=调用方没标注，
    # handshake=握手未完成；未知值构造即抛 ValueError，防止新路径把没登记的位置写进账本。
    _KNOWN_WAIT_PHASES = TIMEOUT_WAIT_PHASES

    def __init__(self, message: str, *, stage: str = "provider_wall", wait_phase: str = ""):
        if stage not in self._KNOWN_STAGES:
            raise ValueError(
                f"未知 ProviderTimeoutError stage: {stage!r} "
                f"(合法: first_event/stream_idle/wall_clock/provider_declared/provider_wall)"
            )
        if wait_phase not in self._KNOWN_WAIT_PHASES:
            raise ValueError(
                f"未知 ProviderTimeoutError wait_phase: {wait_phase!r} "
                f"(合法: '' / handshake)"
            )
        super().__init__(message)
        self.stage = stage
        # 只读诊断字段：默认空串表示“这次超时没有更细的连接位置可标注”，
        # 与“确实是已连接后等待”区分开，读取端不要用空串回推。
        self.wait_phase = wait_phase


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
    if isinstance(exc, ModelNotConfiguredError):
        return f"[model_not_configured]\n{exc}"
    if isinstance(exc, ProviderRequestShapeInvalidError):
        return (
            "[provider_request_shape_invalid]\n"
            "发送前的协议检查发现本次请求的消息结构不合规，请求没有发出，本轮已停止；此前已执行的工作不会因此撤销。\n"
            f"error={exc}\n"
            "这是请求组装的缺陷，请把运行诊断反馈给开发者，不要直接重做整个任务。"
        )
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
