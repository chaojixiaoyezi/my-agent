"""Coercion service: type coercion helpers for config values."""

from __future__ import annotations

import re

_INT_PATTERN = re.compile(r"-?[0-9]+")
_FLOAT_PATTERN = re.compile(r"-?[0-9]+(\.[0-9]+)?")


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
        key: str, value: object, fallback: int, *, min_val: int | None = None, max_val: int | None = None
    ) -> tuple[int, str | None]:
        """Coerce a raw config value to int with optional range checks."""
        if value is None:
            return fallback, None
        if isinstance(value, bool):
            return fallback, f"{key}: expected an integer, got boolean; using {fallback}"
        if isinstance(value, int):
            number = value
        elif isinstance(value, str) and _INT_PATTERN.fullmatch(value.strip()):
            number = int(value.strip())
        elif isinstance(value, float) and value == int(value):
            number = int(value)
        else:
            return fallback, f"{key}: expected an integer, got {value!r}; using {fallback}"
        if min_val is not None and number < min_val:
            return fallback, f"{key}: expected >= {min_val}, got {number}; using {fallback}"
        if max_val is not None and number > max_val:
            return fallback, f"{key}: expected <= {max_val}, got {number}; using {fallback}"
        return number, None

    @staticmethod
    def coerce_float(
        key: str, value: object, fallback: float, *, min_val: float | None = None, max_val: float | None = None
    ) -> tuple[float, str | None]:
        """Coerce a raw config value to float with optional range checks."""
        if value is None:
            return fallback, None
        if isinstance(value, bool):
            return fallback, f"{key}: expected a float, got boolean; using {fallback}"
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            stripped = value.strip()
            if _FLOAT_PATTERN.fullmatch(stripped):
                number = float(stripped)
            else:
                return fallback, f"{key}: expected a float, got {value!r}; using {fallback}"
        else:
            return fallback, f"{key}: expected a float, got {value!r}; using {fallback}"
        if min_val is not None and number < min_val:
            return fallback, f"{key}: expected >= {min_val}, got {number}; using {fallback}"
        if max_val is not None and number > max_val:
            return fallback, f"{key}: expected <= {max_val}, got {number}; using {fallback}"
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