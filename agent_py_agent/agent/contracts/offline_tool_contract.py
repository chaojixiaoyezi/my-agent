
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.value_parsing import sequence_strings
from ..common.value_parsing import text_value as _text
from .contract_validation_recovery import recovery_for_findings

SECRET_FIELD_NAMES = {"api_key", "authorization", "cookie", "password", "secret", "token"}
REDACTED_VALUES = {"[redacted]", "<redacted>", "***", "redacted"}


@dataclass(frozen=True)
class OfflineToolValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


def validate_tool_events(
    events: tuple[dict[str, Any], ...],
    *,
    repeated_threshold: int = 10,
) -> OfflineToolValidation:
    findings: list[dict[str, object]] = []
    _validate_tool_result_shapes(events, findings)
    _validate_repeated_no_progress(events, max(0, repeated_threshold), findings)
    return OfflineToolValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("offline_tool", findings),
    )


def _validate_tool_result_shapes(
    events: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for index, event in enumerate(events):
        if _event_type(event) != "tool_result":
            continue
        result = event.get("result")
        if result is None:
            findings.append(
                _finding(
                    "TOOL_RESULT_NONE",
                    {"index": index, "operation_id": _text(event.get("operation_id"))},
                )
            )
            continue
        if not isinstance(result, dict):
            findings.append(
                _finding(
                    "TOOL_RESULT_SHAPE_INVALID",
                    {"index": index, "operation_id": _text(event.get("operation_id"))},
                )
            )
            continue
        _validate_large_output(index, event, result, findings)
        _validate_secret_fields(index, result, findings)


def _validate_large_output(
    index: int,
    event: dict[str, Any],
    result: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    budget = _optional_int(event.get("inline_budget_bytes") or result.get("inline_budget_bytes"))
    if budget is None:
        return
    inline_size = max(_byte_len(result.get(key)) for key in ("content", "text", "output"))
    if inline_size <= budget:
        return
    if result.get("truncated") is True or sequence_strings(result.get("artifact_refs")):
        return
    findings.append(
        _finding(
            "TOOL_RESULT_TOO_LARGE_NOT_EXTERNALIZED",
            {
                "index": index,
                "operation_id": _text(event.get("operation_id")),
                "inline_size_bytes": inline_size,
                "inline_budget_bytes": budget,
            },
        )
    )


def _validate_secret_fields(
    index: int,
    result: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    for field_path in _secret_field_paths(result, prefix="result"):
        findings.append(_finding("TOOL_RESULT_SECRET_LEAK", {"index": index, "field_path": field_path}))


def _validate_repeated_no_progress(
    events: tuple[dict[str, Any], ...],
    threshold: int,
    findings: list[dict[str, object]],
) -> None:
    if threshold <= 0:
        return
    last_key: tuple[str, str, str] | None = None
    streak = 0
    reported_keys: set[tuple[str, str, str]] = set()
    for index, event in enumerate(events):
        key = _repeat_key(event)
        if key is None:
            last_key = None
            streak = 0
            continue
        streak = streak + 1 if key == last_key else 1
        last_key = key
        if streak >= threshold and key not in reported_keys:
            reported_keys.add(key)
            findings.append(
                _finding(
                    "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED",
                    {
                        "index": index,
                        "tool": key[0],
                        "args_hash": key[1],
                        "result_hash": key[2],
                    },
                )
            )


def _repeat_key(event: dict[str, Any]) -> tuple[str, str, str] | None:
    if _event_type(event) != "tool_result" or event.get("read_only") is not True:
        return None
    tool = _text(event.get("tool"))
    args_hash = _text(event.get("args_hash"))
    result_hash = _text(event.get("result_hash"))
    if not (tool and args_hash and result_hash):
        return None
    return (tool, args_hash, result_hash)


def _secret_field_paths(value: object, *, prefix: str) -> tuple[str, ...]:
    paths: list[str] = []
    stack: list[tuple[str, object]] = [(prefix, value)]
    while stack:
        current_prefix, current = stack.pop()
        if isinstance(current, dict):
            paths.extend(_dict_secret_field_paths(current_prefix, current, stack))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend(_indexed_children(current_prefix, current))
    return tuple(paths)


def _dict_secret_field_paths(
    prefix: str,
    value: dict[object, object],
    stack: list[tuple[str, object]],
) -> list[str]:
    paths: list[str] = []
    for key, child in value.items():
        key_text = _text(key)
        path = f"{prefix}.{key_text}" if prefix else key_text
        if key_text.lower() in SECRET_FIELD_NAMES and not _value_is_redacted(child):
            paths.append(path)
        stack.append((path, child))
    return paths


def _indexed_children(prefix: str, value: list[object] | tuple[object, ...]) -> list[tuple[str, object]]:
    return [(f"{prefix}[{index}]", child) for index, child in enumerate(value)]


def _value_is_redacted(value: object) -> bool:
    if value in (None, ""):
        return True
    if not isinstance(value, str):
        return False
    return value.strip().lower() in REDACTED_VALUES


def _byte_len(value: object) -> int:
    if not isinstance(value, str):
        return 0
    return len(value.encode("utf-8"))


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}


def _event_type(event: dict[str, Any]) -> str:
    return _text(event.get("type")).lower()


__all__ = ["OfflineToolValidation", "validate_tool_events"]
