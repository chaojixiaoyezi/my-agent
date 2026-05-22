# LLM: Offline tool guardrail contracts catch loops and invalid fake-tool results before real runs.
# 模块用途: 校验重复无进展工具调用、失败重试预算和 fake tool 返回形状，防止任务卡死或假成功。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contract_validation_recovery import recovery_for_findings


# LLM: OfflineToolGuardrailValidation reports tool-loop and fake-tool findings.
# 类用途: 返回工具 guardrail 离线合同是否通过、错误码和逐项结构化 finding。
@dataclass(frozen=True)
class OfflineToolGuardrailValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


# LLM: validate_tool_guardrail_events checks structured tool_result events only.
# 函数用途: 用 type/tool/args_hash/result_hash/retryable/result.ok 等机器字段判断循环和重试。
def validate_tool_guardrail_events(
    events: tuple[dict[str, Any], ...],
    *,
    repeated_threshold: int = 3,
    retry_limit: int = 3,
) -> OfflineToolGuardrailValidation:
    findings: list[dict[str, object]] = []
    _validate_result_shapes(events, findings)
    _validate_repeated_exact_results(events, max(2, repeated_threshold), findings)
    _validate_retry_budget(events, max(1, retry_limit), findings)
    return OfflineToolGuardrailValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("offline_tool_guardrail", findings),
    )


# LLM: _validate_result_shapes requires fake tools to return object results with boolean ok.
# 函数用途: 非 dict result 或缺少布尔 ok 时返回 FAKE_TOOL_RESULT_INVALID。
def _validate_result_shapes(
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for index, event in enumerate(events):
        if _event_type(event) != "tool_result":
            continue
        result = event.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            findings.append(_finding("FAKE_TOOL_RESULT_INVALID", index, event))


# LLM: _validate_repeated_exact_results blocks unchanged tool-result streaks.
# 函数用途: 同 tool、args_hash、result_hash 连续达到阈值时返回 TOOL_REPEATED_EXACT_RESULT。
def _validate_repeated_exact_results(
    events: tuple[dict[str, Any], ...],
    threshold: int,
    findings: list[dict[str, object]],
) -> None:
    last_key: tuple[str, str, str] | None = None
    streak = 0
    reported: set[tuple[str, str, str]] = set()
    for index, event in enumerate(events):
        key = _repeat_key(event)
        if key is None:
            last_key = None
            streak = 0
            continue
        streak = streak + 1 if key == last_key else 1
        last_key = key
        if streak >= threshold and key not in reported:
            reported.add(key)
            findings.append(_finding("TOOL_REPEATED_EXACT_RESULT", index, event, {"streak": streak}))


# LLM: _validate_retry_budget blocks retryable failures after the configured budget.
# 函数用途: 连续同 tool/args_hash 的 retryable failure 超出预算时返回 TOOL_RETRY_LIMIT_EXCEEDED。
def _validate_retry_budget(
    events: tuple[dict[str, Any], ...],
    retry_limit: int,
    findings: list[dict[str, object]],
) -> None:
    failures: dict[tuple[str, str], int] = {}
    reported: set[tuple[str, str]] = set()
    for index, event in enumerate(events):
        key = _failure_key(event)
        if key is None:
            continue
        failures[key] = failures.get(key, 0) + 1
        limit = _positive_int(event.get("retry_limit")) or retry_limit
        if failures[key] > limit and key not in reported:
            reported.add(key)
            findings.append(_finding("TOOL_RETRY_LIMIT_EXCEEDED", index, event, {"attempts": failures[key]}))


# LLM: _repeat_key returns exact no-progress identity for tool results.
# 函数用途: 只有 tool、args_hash、result_hash 都存在时才参与重复判断。
def _repeat_key(event: dict[str, Any]) -> tuple[str, str, str] | None:
    if _event_type(event) != "tool_result":
        return None
    tool = _text(event.get("tool"))
    args_hash = _text(event.get("args_hash"))
    result_hash = _text(event.get("result_hash"))
    if not (tool and args_hash and result_hash):
        return None
    return (tool, args_hash, result_hash)


# LLM: _failure_key returns retry identity for failed retryable tool results.
# 函数用途: 只有 result.ok=false 且 retryable=true 时参与失败重试预算。
def _failure_key(event: dict[str, Any]) -> tuple[str, str] | None:
    result = event.get("result")
    if _event_type(event) != "tool_result" or not isinstance(result, dict):
        return None
    if result.get("ok") is not False or event.get("retryable") is not True:
        return None
    tool = _text(event.get("tool"))
    args_hash = _text(event.get("args_hash"))
    if not (tool and args_hash):
        return None
    return (tool, args_hash)


# LLM: _finding creates compact tool guardrail findings.
# 函数用途: 生成 code、index、tool、operation_id 和可选结构化字段。
def _finding(
    code: str,
    index: int,
    event: dict[str, Any],
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "code": code,
        "index": index,
        "tool": _text(event.get("tool")),
        "operation_id": _text(event.get("operation_id")),
        **(extra or {}),
    }


# LLM: _event_type normalizes event type values for exact dispatch.
# 函数用途: 读取 type 字段并转小写字符串。
def _event_type(event: dict[str, Any]) -> str:
    return _text(event.get("type")).lower()


# LLM: _positive_int parses optional numeric limits.
# 函数用途: 将 retry_limit 等字段规整为非负整数。
def _positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


# LLM: _text normalizes optional scalar values for exact comparisons.
# 函数用途: 把 None 或标量转成去空白字符串；不解析自然语言含义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["OfflineToolGuardrailValidation", "validate_tool_guardrail_events"]
