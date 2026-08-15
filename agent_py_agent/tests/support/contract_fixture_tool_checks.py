from __future__ import annotations

from typing import Any


def tool_conditions(
    contract: dict[str, object],
    tool_trace: tuple[dict[str, object], ...],
    evaluation: Any,
) -> dict[str, bool]:
    required_calls = _string_list(_tools_contract(contract).get("required_calls"))
    required_successful_calls = _string_list(_tools_contract(contract).get("required_successful_calls"))
    required_real_successful_calls = _string_list(_tools_contract(contract).get("required_real_successful_calls"))
    if not tool_trace:
        evaluation.add("TOOL_TRACE_EMPTY", "tool_trace", "tool trace is empty")
    missing = [name for name in required_calls if name not in _called_tools(tool_trace)]
    _add_missing_required_calls(evaluation, missing)
    missing_success = [name for name in required_successful_calls if name not in _successful_tools(tool_trace)]
    _add_missing_successful_calls(evaluation, missing_success)
    missing_real_success = [name for name in required_real_successful_calls if name not in _real_successful_tools(tool_trace)]
    _add_missing_real_calls(evaluation, missing_real_success)
    trace_conditions = _tool_trace_conditions(contract, tool_trace, evaluation)
    return {
        **trace_conditions,
        "required_real_tool_calls_present": not missing_real_success
        and bool(tool_trace or not required_real_successful_calls),
        "required_successful_tool_calls_present": not missing_success and bool(tool_trace or not required_successful_calls),
        "required_tool_calls_present": not missing and bool(tool_trace or not required_calls),
    }


def _called_tools(tool_trace: tuple[dict[str, object], ...]) -> set[str]:
    return {str(item.get("tool") or "") for item in tool_trace if isinstance(item, dict)}


def _successful_tools(tool_trace: tuple[dict[str, object], ...]) -> set[str]:
    return {
        str(item.get("tool") or "")
        for item in tool_trace
        if isinstance(item, dict) and bool(_tool_result(item).get("ok"))
    }


def _real_successful_tools(tool_trace: tuple[dict[str, object], ...]) -> set[str]:
    return {
        str(item.get("tool") or "")
        for item in tool_trace
        if isinstance(item, dict) and bool(_tool_result(item).get("ok")) and not _tool_is_dry_run(item)
    }


def _add_missing_required_calls(evaluation: Any, names: list[str]) -> None:
    for name in names:
        evaluation.add("REQUIRED_TOOL_CALL_MISSING", "tool_trace", f"required tool call missing: {name}")


def _add_missing_successful_calls(evaluation: Any, names: list[str]) -> None:
    for name in names:
        evaluation.add(
            "REQUIRED_SUCCESSFUL_TOOL_CALL_MISSING",
            "tool_trace",
            f"required successful tool call missing: {name}",
        )


def _add_missing_real_calls(evaluation: Any, names: list[str]) -> None:
    for name in names:
        evaluation.add(
            "REQUIRED_REAL_TOOL_CALL_MISSING",
            "tool_trace",
            f"required real successful tool call missing: {name}",
        )


def _tool_trace_conditions(
    contract: dict[str, object],
    tool_trace: tuple[dict[str, object], ...],
    evaluation: Any,
) -> dict[str, bool]:
    requirements = _tools_contract(contract).get("trace_requirements")
    if not isinstance(requirements, dict):
        return {"tool_trace_replayable": True, "tool_trace_secrets_redacted": True}
    replayable = _validate_replayable_trace(tool_trace, requirements, evaluation)
    redacted = _validate_trace_redaction(tool_trace, requirements, evaluation)
    return {"tool_trace_replayable": replayable, "tool_trace_secrets_redacted": redacted}


def _validate_replayable_trace(
    tool_trace: tuple[dict[str, object], ...],
    requirements: dict[str, object],
    evaluation: Any,
) -> bool:
    ok = True
    for index, item in enumerate(tool_trace):
        if not isinstance(item, dict):
            evaluation.add("TOOL_TRACE_ITEM_INVALID", f"tool_trace[{index}]", "tool trace item must be object")
            ok = False
            continue
        ok = _validate_trace_item_replayable(index, item, requirements, evaluation) and ok
    return ok


def _validate_trace_item_replayable(
    index: int,
    item: dict[str, object],
    requirements: dict[str, object],
    evaluation: Any,
) -> bool:
    ok = True
    if bool(requirements.get("require_operation_id")) and not _trace_operation_id(item):
        evaluation.add("TOOL_TRACE_OPERATION_ID_MISSING", f"tool_trace[{index}]", "operation id is missing")
        ok = False
    if bool(requirements.get("require_duration_ms")) and _trace_duration_ms(item) is None:
        evaluation.add("TOOL_TRACE_DURATION_MISSING", f"tool_trace[{index}]", "duration_ms is missing")
        ok = False
    if bool(requirements.get("require_error_code_on_failure")) and _trace_failure_without_error_code(item):
        evaluation.add("TOOL_TRACE_ERROR_CODE_MISSING", f"tool_trace[{index}]", "failed tool needs error_code")
        ok = False
    return ok


def _validate_trace_redaction(
    tool_trace: tuple[dict[str, object], ...],
    requirements: dict[str, object],
    evaluation: Any,
) -> bool:
    if not bool(requirements.get("forbid_sensitive_values")):
        return True
    leaks = [location for item in tool_trace for location in _sensitive_value_locations(item)]
    for location in leaks:
        evaluation.add("TOOL_TRACE_SECRET_LEAK", location, "sensitive value is not redacted")
    return not leaks


def _trace_operation_id(item: dict[str, object]) -> str:
    for key in ("operation_id", "tool_call_id", "call_id", "trace_id"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _trace_duration_ms(item: dict[str, object]) -> object:
    if "duration_ms" in item:
        return item.get("duration_ms")
    result = _tool_result(item)
    return result.get("duration_ms") if "duration_ms" in result else None


def _trace_failure_without_error_code(item: dict[str, object]) -> bool:
    result = _tool_result(item)
    if bool(result.get("ok")):
        return False
    return not str(result.get("error_code") or "").strip()


def _tool_is_dry_run(item: dict[str, object]) -> bool:
    result = _tool_result(item)
    params = item.get("params")
    params_dict = dict(params) if isinstance(params, dict) else {}
    mode_values = (item.get("mode"), result.get("mode"), params_dict.get("mode"))
    return bool(item.get("dry_run") or result.get("dry_run") or params_dict.get("dry_run")) or any(
        str(value or "").strip().lower() == "dry_run" for value in mode_values
    )


def _sensitive_value_locations(value: object, *, prefix: str = "tool_trace") -> list[str]:
    if isinstance(value, dict):
        return _sensitive_locations_in_dict(value, prefix=prefix)
    if isinstance(value, list):
        return [
            location
            for index, item in enumerate(value)
            for location in _sensitive_value_locations(item, prefix=f"{prefix}[{index}]")
        ]
    return []


def _sensitive_locations_in_dict(value: dict[object, object], *, prefix: str) -> list[str]:
    locations: list[str] = []
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        location = f"{prefix}.{key}"
        if _key_is_sensitive(key) and not _value_is_redacted(raw_value):
            locations.append(location)
        locations.extend(_sensitive_value_locations(raw_value, prefix=location))
    return locations


def _tool_result(item: dict[str, object]) -> dict[str, object]:
    result = item.get("result")
    return dict(result) if isinstance(result, dict) else {}


def _tools_contract(contract: dict[str, object]) -> dict[str, object]:
    tools = contract.get("tools")
    return dict(tools) if isinstance(tools, dict) else {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for raw in value if (item := str(raw).strip())]


def _key_is_sensitive(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    parts = {part for part in normalized.split("_") if part}
    if parts.intersection(_SENSITIVE_KEY_PARTS):
        return True
    return normalized.replace("_", "") in _SENSITIVE_SQUASHED_KEYS


def _value_is_redacted(value: object) -> bool:
    if value in (None, ""):
        return True
    if not isinstance(value, str):
        return False
    return value.strip().lower() in _REDACTED_VALUES


_SENSITIVE_KEY_PARTS = {"api", "authorization", "cookie", "key", "password", "secret", "token"}
_SENSITIVE_SQUASHED_KEYS = {"apikey", "authorization", "cookie", "password", "secret", "token"}
_REDACTED_VALUES = {"[redacted]", "<redacted>", "***", "redacted"}
