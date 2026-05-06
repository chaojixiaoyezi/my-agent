"""Coercion helpers for memory-related config fields."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ._memory_types import MemoryConfigWarning, MemorySettings

_MISSING = object()
_INT_PATTERN = re.compile(r"-?[0-9]+")


@dataclass(frozen=True)
class _FieldSpec:
    field_name: str
    kind: str
    min_value: int | None = None
    max_value: int | None = None
    choices: set[str] | None = None


@dataclass(frozen=True)
class _WarningDraft:
    field_name: str
    raw_value: Any
    fallback_value: Any
    reason: str


@dataclass(frozen=True)
class _IntCoercion:
    field_name: str
    raw_value: Any
    default: int
    min_value: int
    max_value: int | None


_FIELDS = (
    _FieldSpec("memory_archive_level", "int", 0, 3),
    _FieldSpec("memory_hook_enabled", "bool"),
    _FieldSpec("memory_hook_archive_level", "int", 0, 3),
    _FieldSpec("memory_hook_retention_days", "int", 0, None),
    _FieldSpec("memory_rule_routing_enabled", "bool"),
    _FieldSpec("memory_rule_routing_mode", "choice", choices={"off", "soft", "strict"}),
    _FieldSpec("memory_rule_auto_read_limit", "int", 0, None),
    _FieldSpec("memory_rule_receipt_enabled", "bool"),
    _FieldSpec("memory_resume_auto_context_enabled", "bool"),
    _FieldSpec("memory_resume_auto_context_mode", "choice", choices={"off", "trigger", "always"}),
    _FieldSpec("memory_resume_auto_context_limit", "int", 1, 50),
)


def _build_memory_settings_dict(
    source: Mapping[str, Any] | object,
    defaults: MemorySettings,
    warnings: list[MemoryConfigWarning],
) -> dict[str, Any]:
    """Build a dict of validated memory settings field values."""
    result: dict[str, Any] = {}
    for spec in _FIELDS:
        result[spec.field_name] = _coerce_field(spec, source, defaults, warnings)
    return result


def _coerce_field(
    spec: _FieldSpec,
    source: Mapping[str, Any] | object,
    defaults: MemorySettings,
    warnings: list[MemoryConfigWarning],
) -> Any:
    """Coerce one field spec while preserving user-facing warning order."""
    raw_value = _lookup(source, spec.field_name)
    default = getattr(defaults, spec.field_name)
    if spec.kind == "bool":
        return _coerce_bool(spec.field_name, raw_value, default=default, warnings=warnings)
    if spec.kind == "choice":
        return _coerce_choice(spec.field_name, raw_value, default=default, choices=spec.choices or set(), warnings=warnings)
    return _coerce_int(
        _IntCoercion(
            field_name=spec.field_name,
            raw_value=raw_value,
            default=default,
            min_value=spec.min_value or 0,
            max_value=spec.max_value,
        ),
        warnings=warnings,
    )


def _lookup(source: Mapping[str, Any] | object, field_name: str) -> Any:
    """Read a raw config field from a mapping or dataclass-like object."""
    if isinstance(source, Mapping):
        return source.get(field_name, _MISSING)
    return getattr(source, field_name, _MISSING)


def _warn(
    warnings: list[MemoryConfigWarning],
    draft: _WarningDraft,
) -> None:
    """Append one structured fallback warning."""
    warnings.append(
        MemoryConfigWarning(
            field_name=draft.field_name,
            raw_value=draft.raw_value,
            fallback_value=draft.fallback_value,
            reason=draft.reason,
        )
    )


def _coerce_bool(
    field_name: str,
    raw_value: Any,
    *,
    default: bool,
    warnings: list[MemoryConfigWarning],
) -> bool:
    """Parse a boolean config value and reject ambiguous or injected strings."""
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
    _warn(warnings, _WarningDraft(field_name, raw_value, default, "expected a clear boolean value"))
    return default


def _coerce_choice(
    field_name: str,
    raw_value: Any,
    *,
    default: str,
    choices: set[str],
    warnings: list[MemoryConfigWarning],
) -> str:
    """Parse an enum-like string config value against an allowlist."""
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in choices:
            return normalized
    _warn(warnings, _WarningDraft(field_name, raw_value, default, f"expected one of {sorted(choices)}"))
    return default


def _coerce_int(
    request: _IntCoercion,
    *,
    warnings: list[MemoryConfigWarning],
) -> int:
    """Parse an integer config value using ASCII digits and closed numeric bounds."""
    if request.raw_value is _MISSING:
        return request.default
    number = _memory_int_number(request.raw_value)
    if number is None:
        reason = "expected an integer, not a boolean" if isinstance(request.raw_value, bool) else "expected an integer"
        _warn(warnings, _WarningDraft(request.field_name, request.raw_value, request.default, reason))
        return request.default
    if number < request.min_value:
        reason = f"expected value >= {request.min_value}"
        _warn(warnings, _WarningDraft(request.field_name, request.raw_value, request.default, reason))
        return request.default
    if request.max_value is not None and number > request.max_value:
        reason = f"expected value <= {request.max_value}"
        _warn(warnings, _WarningDraft(request.field_name, request.raw_value, request.default, reason))
        return request.default
    return number


def _memory_int_number(raw_value: Any) -> int | None:
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, int):
        return raw_value
    if isinstance(raw_value, str) and _INT_PATTERN.fullmatch(raw_value.strip()):
        return int(raw_value.strip())
    return None
