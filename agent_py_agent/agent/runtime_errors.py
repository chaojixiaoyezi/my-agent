"""Structured runtime error reports for recoverable agent bookkeeping failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .backends.errors import (
    ProviderConfigurationError,
    ProviderRecoverableError,
    ProviderResponseError,
    is_provider_timeout_error,
    is_provider_transient_error,
)

_MAX_ERROR_TEXT = 300

_SUBAGENT_LEDGER_CONTEXTS = {
    "background_dispatch.agent_tree",
    "cancel_subagents.load",
    "subagents.load",
    "subagents.list_runs",
    "schedule_child_subagents.load",
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


def runtime_error_report(exc: BaseException, *, context: str = "") -> dict[str, Any]:
    """Return the canonical small report for model-visible recoverable errors."""
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
    if isinstance(exc, ProviderRecoverableError):
        return _report(exc, _provider_supply_template(exc), context=context)
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
    return _report(
        exc,
        RuntimeErrorTemplate(
            category="programmer_bug",
            recoverable=False,
            model_message="系统代码异常；不要把它解释成任务完成或子代理没有结果。",
            operator_message="unexpected code exception",
        ),
        context=context,
    )


def compact_error_message(exc: BaseException, *, max_chars: int = _MAX_ERROR_TEXT) -> str:
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
