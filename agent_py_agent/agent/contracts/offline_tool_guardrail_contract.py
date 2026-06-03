
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.value_parsing import text_value as _text
from .contract_validation_recovery import recovery_for_findings


@dataclass(frozen=True)
class OfflineToolGuardrailValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


def validate_tool_guardrail_events(
    events: tuple[dict[str, Any], ...],
    *,
    repeated_threshold: int = 3,
    retry_limit: int = 3,
) -> OfflineToolGuardrailValidation:
    findings: list[dict[str, object]] = []
    _validate_result_shapes(events, findings)
    _validate_repeated_exact_results(events, max(0, repeated_threshold), findings)
    _validate_retry_budget(events, max(0, retry_limit), findings)
    return OfflineToolGuardrailValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("offline_tool_guardrail", findings),
    )


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


def _validate_repeated_exact_results(
    events: tuple[dict[str, Any], ...],
    threshold: int,
    findings: list[dict[str, object]],
) -> None:
    if threshold <= 0:
        return
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
        limit = _event_retry_limit(event, retry_limit)
        if limit > 0 and failures[key] > limit and key not in reported:
            reported.add(key)
            findings.append(_finding("TOOL_RETRY_LIMIT_EXCEEDED", index, event, {"attempts": failures[key]}))


def _repeat_key(event: dict[str, Any]) -> tuple[str, str, str] | None:
    if _event_type(event) != "tool_result":
        return None
    tool = _text(event.get("tool"))
    args_hash = _text(event.get("args_hash"))
    result_hash = _text(event.get("result_hash"))
    if not (tool and args_hash and result_hash):
        return None
    return (tool, args_hash, result_hash)


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


def _event_type(event: dict[str, Any]) -> str:
    return _text(event.get("type")).lower()


def _positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _event_retry_limit(event: dict[str, Any], default: int) -> int:
    if "retry_limit" not in event:
        return default
    return _positive_int(event.get("retry_limit"))

__all__ = ["OfflineToolGuardrailValidation", "validate_tool_guardrail_events"]
