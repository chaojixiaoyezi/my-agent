# LLM: 统一运行错误报告供主子代理及持久事实消费；typed 请求拒绝不是配置错误，文案不能扩展恢复权限。
# 模块用途: 把异常转成模型和运维能理解的分类，不把一次拒绝说成此前工作未执行。

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from .backends.errors import (
    ModelNotConfiguredError,
    ProviderConfigurationError,
    ProviderRecoverableError,
    ProviderRequestRejectedError,
    ProviderRequestShapeInvalidError,
    ProviderResponseError,
    is_provider_timeout_error,
    is_provider_transient_error,
    provider_error_http_status,
)
from .contracts.model_call_ledger import (
    ModelCallAdmissionClosedError,
    find_model_call_admission_error,
)
from .runtime_db.operations import RuntimeExecutionBusyError

# 错误文本最多回 300 字符：超长截断，防错误信息撑爆回执。
_MAX_ERROR_TEXT_CHARS = 300
# 结构化错误码的唯一形状：大写字母开头，只含大写字母、数字和下划线。
STRUCTURED_ERROR_CODE_PATTERN = re.compile(r"[A-Z][A-Z0-9_]+")

_SUBAGENT_LEDGER_CONTEXTS = {
    "background_dispatch.agent_tree",
    "cancel_subagents.load",
    "subagents.load",
    "subagents.list_runs",
    "create_subagents.nested_load",
}
_SUBAGENT_LEDGER_CONTEXT_SUFFIXES = (
    ".subagents.load",
    ".subagents.save",
    ".subagents.list_runs",
    ".child_status.load",
)
_GUIDANCE_CONTEXT_PREFIXES = (
    "conversation.guidance.",
    "runtime_guidance.",
    "agent_tree.guidance.",
    "send_guidance.",
    "dispatch.guidance.",
)


# LLM: 判定只看形状，不看码是否登记过（开放世界）；空白、小写、带空格或明细的字符串都不算结构化码。
#   SkillSnapshotError 构造和唤醒毒丸的原因码都用这一条规则，改动时联查 test_skill_snapshot_error_codes 与 test_wake_poison。
# 函数用途: 判断一个值是不是合规的结构化错误码。
def is_structured_error_code(value: object) -> bool:
    return isinstance(value, str) and STRUCTURED_ERROR_CODE_PATTERN.fullmatch(value) is not None


class RecoverableRuntimeError(RuntimeError):
    """A runtime failure that should be reported to the model or parent agent."""

    category = "recoverable_runtime"


class DataCorruptionError(RecoverableRuntimeError):
    """A persisted ledger/state file is unreadable or internally inconsistent."""

    category = "data_corruption"


class ProgrammerBug(RuntimeError):
    """A non-recoverable code bug that should not be hidden as empty state."""


@dataclass(frozen=True)
class RuntimeErrorReport:
    category: str
    error_type: str
    message: str
    recoverable: bool
    model_message: str
    operator_message: str
    context: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "category": self.category,
            "error_type": self.error_type,
            "message": self.message,
            "recoverable": self.recoverable,
            "model_message": self.model_message,
            "operator_message": self.operator_message,
        }
        if self.context:
            payload["context"] = self.context
        return payload


@dataclass(frozen=True)
class RuntimeErrorTemplate:
    category: str
    recoverable: bool
    model_message: str
    operator_message: str


# LLM: 分类优先遵循异常类型；请求拒绝必须在配置父类前处理，本地形状错误（请求拒绝的子类）又在请求拒绝之前；
#   宿主停机准入拒绝（异常本身或显式原因链里）最先判，报 host_stopping，不能掉进 programmer_bug 兜底；
#   调用方不得从 model_message 推断重试或执行结果。
# 函数用途: 生成主代理、子代理和运行账共用的小型错误报告，不推测服务端未返回的拒绝原因。
def runtime_error_report(exc: BaseException, *, context: str = "") -> dict[str, Any]:
    """Return the canonical small report for model-visible recoverable errors."""
    admission = find_model_call_admission_error(exc)
    if admission is not None:
        return _host_stopping_report(exc, admission, context=context)
    if isinstance(exc, DataCorruptionError):
        return _report(
            exc,
            _template(
                "data_corruption",
                _context_model_message(
                    context,
                    default="账本或状态文件读取失败；请先刷新状态或重建索引，不要把它当成没有数据。",
                ),
                "persistent agent ledger/state is corrupted or unreadable",
            ),
            context=context,
        )
    if isinstance(exc, RecoverableRuntimeError):
        return _report(
            exc,
            _template(
                getattr(exc, "category", "recoverable_runtime"),
                "运行时读取失败；请根据错误类型刷新状态、重试读取或请求上级接管。",
                "recoverable runtime failure",
            ),
            context=context,
        )
    if isinstance(exc, RuntimeExecutionBusyError):
        return _report(
            exc,
            _template(
                "runtime_busy",
                "当前任务已有存活执行者持有执行权；系统会保留待办并稍后重试，不要把它当成任务失败。",
                "live execution owner holds the run lock; retry after it yields",
            ),
            context=context,
        )
    if isinstance(exc, ProviderRecoverableError):
        return _report(exc, _provider_supply_template(exc), context=context)
    if isinstance(exc, ProviderRequestRejectedError):
        return _request_rejected_report(exc, context=context)
    if isinstance(exc, ModelNotConfiguredError):
        return _report(
            exc,
            _template("model_not_configured", str(exc), "model has not been configured", recoverable=False),
            context=context,
        )
    if isinstance(exc, ProviderConfigurationError):
        return _report(
            exc,
            _template(
                "provider_configuration",
                "模型服务配置不可用；请检查接口地址、模型名称和密钥组合后重试。",
                "provider endpoint/model/credential configuration rejected",
                recoverable=False,
            ),
            context=context,
        )
    if isinstance(exc, (OSError, UnicodeError, ValueError)):
        return _report(
            exc,
            _template(
                _builtin_category(exc),
                _local_failure_model_message(context),
                "local read/parse failure",
            ),
            context=context,
        )
    report = _report(
        exc,
        RuntimeErrorTemplate(
            category="programmer_bug",
            recoverable=False,
            model_message="系统代码异常；不要把它解释成任务完成或子代理没有结果。",
            operator_message="unexpected code exception",
        ),
        context=context,
    )
    # 包装异常（如 SchedulerDueIndexError）的显式原因链里若是环境错误，只补归因字段，不改 category：
    # category 参与控制流（取消工具等），环境归因只给循环错误打印与账本看。
    report.update(environment_cause_fields(exc))
    return report


# LLM: 宿主停机关闭模型调用准入后，新调用登记时被拒：请求没有发出，不是代码错误也不是供应商失败。
#   原因码只取结构化属性：error_code=MODEL_CALL_ADMISSION_CLOSED（异常的固定码），reason_code=关门原因的错误码
#   （Gateway 停机是 MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN）；不读异常文本。本进程内不可重试（recoverable=False）。
# 函数用途: 生成“宿主正在停机、模型调用没有发出”的错误报告，并带上结构化原因码。
def _host_stopping_report(
    exc: BaseException,
    admission: ModelCallAdmissionClosedError,
    *,
    context: str,
) -> dict[str, Any]:
    report = _report(
        exc,
        _template(
            "host_stopping",
            "宿主正在停机，本进程已不再接新的模型调用，这次请求没有发出；此前已执行的工作不会因此撤销。"
            "不要在本进程里重试；重启后不会自动续跑这次被拒的回合或子代理 run，需要时由用户重发或父级按恢复决定续派。",
            "host is shutting down; model call admission closed, nothing was sent",
            recoverable=False,
        ),
        context=context,
    )
    report["error_code"] = admission.error_code
    report["reason_code"] = admission.closure.error_code
    return report


# 环境类原因：进程外部条件导致的失败（磁盘满、权限、IO、SQLite 运行期错误如 disk full/locked/unable to open），
# 不是代码逻辑错误。只看异常类型，不看消息文本，也不特判某个 errno。
_ENVIRONMENT_CAUSE_TYPES: tuple[type[BaseException], ...] = (OSError, sqlite3.OperationalError)


# LLM: 本地形状错误（出站协议合同在发送前拦下，请求根本没发出）是请求拒绝的子类，必须先判，不能套"模型服务拒绝了本次请求"
#   的文案；服务端拒绝才带合法的 HTTP 状态码。只读异常类型和结构化状态码，不读正文。
# 函数用途: 生成请求拒绝与发送前形状错误两类报告，供 runtime_error_report 调用。
def _request_rejected_report(exc: ProviderRequestRejectedError, *, context: str) -> dict[str, Any]:
    if isinstance(exc, ProviderRequestShapeInvalidError):
        return _report(
            exc,
            _template(
                "provider_request_shape_invalid",
                "发送前的协议检查发现本次请求的消息结构不合规，请求没有发出；此前已执行的工作不会因此撤销。"
                "这是请求组装的缺陷，不要原样重试。",
                "request blocked locally by the outbound wire contract; nothing was sent",
                recoverable=False,
            ),
            context=context,
        )
    report = _report(
        exc,
        _template(
            "provider_request_rejected",
            "模型服务拒绝了本次请求；具体原因尚未确定，请查看请求诊断。此前已执行的工作不会因此撤销。",
            "provider rejected this request; cause is not established",
            recoverable=False,
        ),
        context=context,
    )
    status = provider_error_http_status(exc)
    if status is not None:
        report["http_status"] = status
    return report


# LLM: 只沿显式 __cause__（raise ... from）往里走，不沿 __context__：except/finally 里顺带发生的程序错误（KeyError、
#   AttributeError）会带着前一个 OSError 的 __context__，沿它走会把程序错误误归成环境。有环 seen 防护。
# 函数用途: 从包装异常的显式原因链里找出环境类根因，没有就返回 None。
def _environment_cause(exc: BaseException) -> BaseException | None:
    seen: set[int] = set()
    current: BaseException | None = exc.__cause__
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _ENVIRONMENT_CAUSE_TYPES):
            return current
        current = current.__cause__
    return None


# LLM: 报告与账本共用的环境归因字段：cause_type 是根因类型名，cause_category 是它按 _builtin_category 的类别（OSError/
#   sqlite3.OperationalError 都是 io）。不改外层 category；没有环境根因返回空字典，调用方 update 即可保持 schema 稳定。
# 函数用途: 给包装异常补"根因是什么环境错误"两个字段。
def environment_cause_fields(exc: BaseException) -> dict[str, str]:
    cause = _environment_cause(exc)
    if cause is None:
        return {}
    return {"cause_type": cause.__class__.__name__, "cause_category": _builtin_category(cause)}


def compact_error_message(exc: BaseException, *, max_chars: int = _MAX_ERROR_TEXT_CHARS) -> str:
    text = str(exc or "").strip() or exc.__class__.__name__
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... 已截断"


def _report(
    exc: BaseException,
    template: RuntimeErrorTemplate,
    *,
    context: str,
) -> dict[str, Any]:
    return RuntimeErrorReport(
        category=template.category,
        error_type=exc.__class__.__name__,
        message=compact_error_message(exc),
        recoverable=template.recoverable,
        model_message=template.model_message,
        operator_message=template.operator_message,
        context=str(context or "").strip(),
    ).to_dict()


# LLM: Error templates carry an explicit recoverability bit so permanent provider/configuration facts cannot inherit the recoverable default by accident.
# 函数用途: 构造统一错误报告模板；只有调用方明确传入时才把错误标为不可恢复。
def _template(
    category: str,
    model_message: str,
    operator_message: str,
    *,
    recoverable: bool = True,
) -> RuntimeErrorTemplate:
    return RuntimeErrorTemplate(
        category=category,
        recoverable=recoverable,
        model_message=model_message,
        operator_message=operator_message,
    )


# 模型供应侧临时错(429 限流/断供/超时/坏响应)是【可恢复】的环境故障:额度会刷新、服务会
# 回来。此前它既不是 RecoverableRuntimeError 也不是 OSError,掉进 programmer_bug 兜底被判
# recoverable=False → 后台循环当致命错放弃,额度恢复也无人续跑(真机实锤)。判据只用异常
# 类型(backends/errors 的 typed 家族),不做任何文本匹配。
def _provider_supply_template(exc: BaseException) -> RuntimeErrorTemplate:
    return _template(
        _provider_category(exc),
        "模型接口临时不可用（限流/断供/超时）；这是外部供应临时故障，系统会自动退避重试，"
        "不要把它当成任务失败、任务完成或没有数据。",
        "temporary model-provider failure; retry with backoff, not a code bug",
    )


def _provider_category(exc: BaseException) -> str:
    if is_provider_timeout_error(exc):
        return "provider_timeout"
    if is_provider_transient_error(exc):
        return "provider_transient"
    if isinstance(exc, ProviderResponseError):
        return "provider_response"
    return "provider_recoverable"


def _builtin_category(exc: BaseException) -> str:
    if isinstance(exc, ValueError):
        return "data_parse"
    if isinstance(exc, UnicodeError):
        return "data_encoding"
    return "io"


def _local_failure_model_message(context: str) -> str:
    return _context_model_message(
        context,
        default="本地状态或路径读取失败；请检查路径、权限、编码或刷新状态后继续。",
    )


def _context_model_message(context: str, *, default: str) -> str:
    normalized = str(context or "").strip()
    if _is_subagent_ledger_context(normalized):
        return "子代理账本读取失败；请刷新代理树或重建索引，不要把它当成子代理没产物。"
    if _is_guidance_context(normalized):
        return "运行中补充提示读取失败；请刷新会话或提示账本，不要把它当成没有用户补充提示。"
    return default


def _is_subagent_ledger_context(context: str) -> bool:
    return context in _SUBAGENT_LEDGER_CONTEXTS or any(
        context.endswith(suffix) for suffix in _SUBAGENT_LEDGER_CONTEXT_SUFFIXES
    )


def _is_guidance_context(context: str) -> bool:
    return any(context.startswith(prefix) for prefix in _GUIDANCE_CONTEXT_PREFIXES)
