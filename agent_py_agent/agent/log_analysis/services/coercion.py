
"""Type coercion and field normalization utilities for log analysis config.

These functions safely convert raw YAML values into validated typed values,
emitting warnings when values fall back to defaults due to invalid input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..config import LogAnalysisConfigWarning

_MISSING = object()
_INT_PATTERN = re.compile(r"-?[0-9]+")


@dataclass(frozen=True)
class ConfigWarningInput:
    field_name: str
    raw_value: Any
    default_value: Any
    reason: str


@dataclass(frozen=True)
class ChoiceCoercionOptions:
    default: str
    choices: set[str]
    uppercase: bool = False


@dataclass(frozen=True)
class IntCoercionOptions:
    default: int
    min_value: int
    max_value: int | None


def lookup(source: dict[str, Any] | object, field_name: str) -> Any:
    """Read a config field from a Mapping or plain object, returning _MISSING sentinel if absent."""
    if isinstance(source, dict):
        return source.get(field_name, _MISSING)
    return getattr(source, field_name, _MISSING)


def append_warning(
    warnings: list[LogAnalysisConfigWarning],
    params: ConfigWarningInput | None = None,
    *,
    warning: ConfigWarningInput | None = None,
) -> None:
    """Append a configuration warning entry."""
    item = params or warning
    if item is None:
        raise TypeError("append_warning requires params")
    warnings.append(
        LogAnalysisConfigWarning(
            field_name=item.field_name,
            raw_value=item.raw_value,
            default_value=item.default_value,
            reason=item.reason,
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

    Accepts bool, 0/1 int, and strings "true"/"false", "yes"/"no", "on"/"off"."""
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
    append_warning(warnings, ConfigWarningInput(field_name, raw_value, default, "expected a clear boolean value"))
    return default


def coerce_choice(
    field_name: str,
    raw_value: Any,
    *,
    default: str,
    choices: set[str],
    warnings: list[LogAnalysisConfigWarning],
    uppercase: bool = False,
    options: ChoiceCoercionOptions | None = None,
) -> str:
    """Coerce a value to a string chosen from an allowed set.

    If uppercase=True, normalize input to upper-case before comparing."""
    coercion = options or ChoiceCoercionOptions(default=str(default), choices=set(choices), uppercase=uppercase)
    if raw_value is _MISSING:
        return coercion.default
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        normalized = normalized.upper() if coercion.uppercase else normalized.lower()
        if normalized in coercion.choices:
            return normalized
    append_warning(warnings, ConfigWarningInput(field_name, raw_value, coercion.default, f"expected one of {sorted(coercion.choices)}"))
    return coercion.default


def coerce_int(
    field_name: str,
    raw_value: Any,
    *,
    default: int,
    min_value: int,
    warnings: list[LogAnalysisConfigWarning],
    max_value: int | None = None,
    options: IntCoercionOptions | None = None,
) -> int:
    """Coerce a value to an integer within [min_value, max_value].

    Rejects booleans explicitly (since bool is int in Python) and non-integer strings."""
    coercion = options or IntCoercionOptions(default=int(default), min_value=int(min_value), max_value=max_value)
    if raw_value is _MISSING:
        return coercion.default
    if isinstance(raw_value, bool):
        append_warning(
            warnings,
            ConfigWarningInput(field_name, raw_value, coercion.default, "expected an integer, not a boolean"),
        )
        return coercion.default
    if isinstance(raw_value, int):
        number = raw_value
    elif isinstance(raw_value, str) and _INT_PATTERN.fullmatch(raw_value.strip()):
        number = int(raw_value.strip())
    else:
        append_warning(warnings, ConfigWarningInput(field_name, raw_value, coercion.default, "expected an integer"))
        return coercion.default

    if number < coercion.min_value:
        append_warning(
            warnings,
            ConfigWarningInput(field_name, raw_value, coercion.default, f"expected value >= {coercion.min_value}"),
        )
        return coercion.default
    if coercion.max_value is not None and number > coercion.max_value:
        append_warning(
            warnings,
            ConfigWarningInput(field_name, raw_value, coercion.default, f"expected value <= {coercion.max_value}"),
        )
        return coercion.default
    return number


def coerce_path_string(
    field_name: str,
    raw_value: Any,
    *,
    default: str,
    warnings: list[LogAnalysisConfigWarning],
) -> str:
    """Coerce a value to a non-empty, safe path string.

    Rejects null bytes and control characters; preserves empty strings as invalid."""
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        if normalized and "\x00" not in normalized and "\n" not in normalized and "\r" not in normalized:
            return normalized
    append_warning(warnings, ConfigWarningInput(field_name, raw_value, default, "expected a non-empty path string"))
    return default
