"""Coercion service: type coercion helpers for config values."""

from __future__ import annotations

import re
from dataclasses import dataclass

_INT_PATTERN = re.compile(r"-?[0-9]+")
_FLOAT_PATTERN = re.compile(r"-?[0-9]+(\.[0-9]+)?")


@dataclass(frozen=True)
class _Bounds:
    min_val: int | float | None
    max_val: int | float | None


@dataclass(frozen=True)
class CoerceNumberParams:
    # LLM: config number coercion bounds travel together to keep facade signatures stable.
    min_val: int | float | None = None
    max_val: int | float | None = None


class CoercionService:
    """Service for coercing raw config values to typed values with safe fallbacks."""

    @staticmethod
    def coerce_bool(key: str, value: object, fallback: bool) -> tuple[bool, str | None]:
        """Coerce a raw config value to bool with a safe fallback."""
        if value is None:
            return fallback, None
        if isinstance(value, bool):
            return value, None
        if isinstance(value, int) and value in {0, 1}:
            return bool(value), None
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "on", "1"}:
                return True, None
            if normalized in {"false", "no", "off", "0"}:
                return False, None
        return fallback, f"{key}: expected a clear boolean, got {value!r}; using {fallback}"

    @staticmethod
    def coerce_int(
        key: str,
        value: object,
        fallback: int,
        *,
        params: CoerceNumberParams | None = None,
        min_val: int | None = None,
        max_val: int | None = None,
    ) -> tuple[int, str | None]:
        """Coerce a raw config value to int with optional range checks."""
        bounds = params or CoerceNumberParams(min_val, max_val)
        if value is None:
            return fallback, None
        number = _coerce_int_number(value)
        if number is None:
            detail = "boolean" if isinstance(value, bool) else repr(value)
            return fallback, f"{key}: expected an integer, got {detail}; using {fallback}"
        warn = _range_warning(key, number, fallback, _Bounds(bounds.min_val, bounds.max_val))
        if warn:
            return fallback, warn
        return number, None

    @staticmethod
    def coerce_float(
        key: str,
        value: object,
        fallback: float,
        *,
        params: CoerceNumberParams | None = None,
        min_val: float | None = None,
        max_val: float | None = None,
    ) -> tuple[float, str | None]:
        """Coerce a raw config value to float with optional range checks."""
        bounds = params or CoerceNumberParams(min_val, max_val)
        if value is None:
            return fallback, None
        number = _coerce_float_number(value)
        if number is None:
            detail = "boolean" if isinstance(value, bool) else repr(value)
            return fallback, f"{key}: expected a float, got {detail}; using {fallback}"
        warn = _range_warning(key, number, fallback, _Bounds(bounds.min_val, bounds.max_val))
        if warn:
            return fallback, warn
        return number, None

    @staticmethod
    def coerce_choice(key: str, value: object, fallback: str, choices: tuple[str, ...]) -> tuple[str, str | None]:
        """Coerce a raw config value to one of the allowed string choices."""
        if value is None:
            return fallback, None
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in choices:
                return normalized, None
        return fallback, f"{key}: expected one of {list(choices)}, got {value!r}; using {fallback}"


def _coerce_int_number(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INT_PATTERN.fullmatch(value.strip()):
        return int(value.strip())
    if isinstance(value, float) and value == int(value):
        return int(value)
    return None


def _coerce_float_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not _FLOAT_PATTERN.fullmatch(stripped):
        return None
    return float(stripped)


def _range_warning(
    key: str,
    number: int | float,
    fallback: int | float,
    bounds: _Bounds,
) -> str | None:
    if bounds.min_val is not None and number < bounds.min_val:
        return f"{key}: expected >= {bounds.min_val}, got {number}; using {fallback}"
    if bounds.max_val is not None and number > bounds.max_val:
        return f"{key}: expected <= {bounds.max_val}, got {number}; using {fallback}"
    return None
