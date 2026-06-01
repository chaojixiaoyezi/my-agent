"""Structured runtime error reports for recoverable agent bookkeeping failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_MAX_ERROR_TEXT = 300


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
                "账本或状态文件读取失败；请先刷新状态或让父代理重建索引，不要把它当成子代理没产物。",
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


def _template(category: str, model_message: str, operator_message: str) -> RuntimeErrorTemplate:
    return RuntimeErrorTemplate(
        category=category,
        recoverable=True,
        model_message=model_message,
        operator_message=operator_message,
    )


def _builtin_category(exc: BaseException) -> str:
    if isinstance(exc, ValueError):
        return "data_parse"
    if isinstance(exc, UnicodeError):
        return "data_encoding"
    return "io"


def _local_failure_model_message(context: str) -> str:
    if str(context or "").strip() == "subagents.load":
        return "子代理账本读取失败；请刷新代理树或重建索引，不要把它当成子代理没产物。"
    return "本地状态或路径读取失败；请检查路径、权限、编码或刷新代理树后继续。"
