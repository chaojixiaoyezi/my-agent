"""Type coercion and field normalization utilities for log analysis config.

These functions safely convert raw YAML values into validated typed values,
emitting warnings when values fall back to defaults due to invalid input.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import LogAnalysisConfigWarning

_MISSING = object()
_INT_PATTERN = re.compile(r"-?[0-9]+")


def lookup(source: dict[str, Any] | object, field_name: str) -> Any:
    """Read a config field from a Mapping or plain object, returning _MISSING sentinel if absent."""
    if isinstance(source, dict):
        return source.get(field_name, _MISSING)
    return getattr(source, field_name, _MISSING)


def append_warning(
    warnings: list[LogAnalysisConfigWarning],
    field_name: str,
    raw_value: Any,
    fallback_value: Any,
    reason: str,
) -> None:
    """Append a configuration warning entry."""
    warnings.append(
        LogAnalysisConfigWarning(
            field_name=field_name,
            raw_value=raw_value,
            fallback_value=fallback_value,
            reason=reason,
        )
    )


def coerce_bool(
    field_name: str,
    raw_value: Any,
    *,
    default: bool,
    warnings: list[LogAnalysisConfigWarning],
) -> bool:
    """Coerce a value to bool, falling back to default on bad input.

    Accepts bool, 0/1 int, and strings "true"/"false", "yes"/"no", "on"/"off".
    """
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, int) and raw_value in {0, 1}:
        return bool(raw_value)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    append_warning(warnings, field_name, raw_value, default, "expected a clear boolean value")
    return default


def coerce_choice(
    field_name: str,
    raw_value: Any,
    *,
    default: str,
    choices: set[str],
    warnings: list[LogAnalysisConfigWarning],
    uppercase: bool = False,
) -> str:
    """Coerce a value to a string chosen from an allowed set.

    If uppercase=True, normalize input to upper-case before comparing.
    """
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        normalized = normalized.upper() if uppercase else normalized.lower()
        if normalized in choices:
            return normalized
    append_warning(warnings, field_name, raw_value, default, f"expected one of {sorted(choices)}")
    return default


def coerce_int(
    field_name: str,
    raw_value: Any,
    *,
    default: int,
    min_value: int,
    max_value: int | None,
    warnings: list[LogAnalysisConfigWarning],
) -> int:
    """Coerce a value to an integer within [min_value, max_value].

    Rejects booleans explicitly (since bool is int in Python) and non-integer strings.
    """
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, bool):
        append_warning(warnings, field_name, raw_value, default, "expected an integer, not a boolean")
        return default
    if isinstance(raw_value, int):
        number = raw_value
    elif isinstance(raw_value, str) and _INT_PATTERN.fullmatch(raw_value.strip()):
        number = int(raw_value.strip())
    else:
        append_warning(warnings, field_name, raw_value, default, "expected an integer")
        return default

    if number < min_value:
        append_warning(warnings, field_name, raw_value, default, f"expected value >= {min_value}")
        return default
    if max_value is not None and number > max_value:
        append_warning(warnings, field_name, raw_value, default, f"expected value <= {max_value}")
        return default
    return number


def coerce_path_string(
    field_name: str,
    raw_value: Any,
    *,
    default: str,
    warnings: list[LogAnalysisConfigWarning],
) -> str:
    """Coerce a value to a non-empty, safe path string.

    Rejects null bytes and control characters; preserves empty strings as invalid.
    """
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        if normalized and "\x00" not in normalized and "\n" not in normalized and "\r" not in normalized:
            return normalized
    append_warning(warnings, field_name, raw_value, default, "expected a non-empty path string")
    return default