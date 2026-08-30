from __future__ import annotations

from agent_py_agent.agent.backends.errors import (
    ProviderContextWindowError,
    ProviderRequestRejectedError,
    ProviderTimeoutError,
    ProviderTransientError,
)
from agent_py_agent.agent.runtime_db.operations import RuntimeExecutionBusyError
from agent_py_agent.agent.runtime_errors import DataCorruptionError, runtime_error_report


def test_runtime_error_report_uses_structured_context_for_subagent_message() -> None:
    payload = runtime_error_report(OSError("ledger unreadable"), context="tool_call_scope.subagents.load")

    assert payload["context"] == "tool_call_scope.subagents.load"
    assert "子代理账本读取失败" in payload["model_message"]


def test_runtime_error_report_does_not_infer_subagent_context_from_free_text() -> None:
    payload = runtime_error_report(OSError("ledger unreadable"), context="please load the subagent note")

    assert "子代理账本读取失败" not in payload["model_message"]
    assert "本地状态或路径读取失败" in payload["model_message"]


def test_runtime_error_report_uses_structured_context_for_guidance_message() -> None:
    payload = runtime_error_report(OSError("guidance unreadable"), context="conversation.guidance.read")

    assert "运行中补充提示读取失败" in payload["model_message"]


def test_data_corruption_report_uses_generic_message_without_matching_context() -> None:
    payload = runtime_error_report(DataCorruptionError("bad json"), context="memory.routing.load")

    assert "子代理没产物" not in payload["model_message"]
    assert "没有数据" in payload["model_message"]


def test_provider_rate_limit_is_recoverable_supply_error_not_programmer_bug() -> None:
    """真机实锤回归锁:429 限流(ProviderTransientError)是临时供应错,必须判【可恢复】。
    修复前它掉进兜底被标 programmer_bug/recoverable=False → 后台循环当致命错放弃,
    额度刷新后也无人续跑(1.9 冻死 / 1.10 死透)。撤掉 runtime_errors 的 provider 分支即 FAIL。"""
    payload = runtime_error_report(
        ProviderTransientError('HTTP 429: rate_limit_error "已达到 Token Plan 用量上限"'),
        context="gateway_background_main.iteration",
    )

    assert payload["recoverable"] is True
    assert payload["category"] == "provider_transient"
    assert payload["category"] != "programmer_bug"
    assert payload["error_type"] == "ProviderTransientError"
    assert "unexpected code exception" not in payload["operator_message"]


def test_provider_timeout_and_response_errors_are_recoverable() -> None:
    timeout = runtime_error_report(ProviderTimeoutError("request timed out"))
    assert timeout["recoverable"] is True
    assert timeout["category"] == "provider_timeout"

    context_window = runtime_error_report(ProviderContextWindowError("prompt too large"))
    assert context_window["recoverable"] is True
    assert context_window["category"] == "provider_response"


def test_provider_request_rejection_is_configuration_error_not_programmer_bug() -> None:
    payload = runtime_error_report(
        ProviderRequestRejectedError("HTTP 401: model rejected", status_code=401)
    )

    assert payload["recoverable"] is False
    assert payload["category"] == "provider_configuration"
    assert payload["error_type"] == "ProviderRequestRejectedError"
    assert payload["category"] != "programmer_bug"


def test_live_execution_lock_conflict_is_recoverable_busy_not_programmer_bug() -> None:
    payload = runtime_error_report(
        RuntimeExecutionBusyError("another live attempt owns the lock"),
        context="gateway_background_main.iteration",
    )

    assert payload["recoverable"] is True
    assert payload["category"] == "runtime_busy"
    assert payload["error_type"] == "RuntimeExecutionBusyError"
    assert payload["category"] != "programmer_bug"


def test_unexpected_exception_is_still_programmer_bug() -> None:
    """不回归:真·致命程序 bug 仍走 programmer_bug 兜底,不能把所有错放行成可恢复。"""
    payload = runtime_error_report(RuntimeError("attribute lookup exploded"))

    assert payload["recoverable"] is False
    assert payload["category"] == "programmer_bug"
