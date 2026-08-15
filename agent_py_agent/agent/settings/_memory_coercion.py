"""Coercion helpers for memory-related config fields."""

# LLM: Every Memory config field must normalize through this typed table; invalid values warn and use one declared default.
# 模块用途: 校验 Memory/Curator 配置类型、枚举与范围，并生成不含秘密的结构化警告。

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ._memory_types import MemoryConfigWarning, MemorySettings

_MISSING = object()
_INT_PATTERN = re.compile(r"-?[0-9]+")


# LLM: Field specs are the sole coercion rules; max_chars bounds provider/model identifiers without parsing semantics.
# 类用途: 描述一个 Memory 配置字段的类型、范围、枚举或长度限制。
@dataclass(frozen=True)
class _FieldSpec:
    field_name: str
    kind: str
    min_value: int | None = None
    max_value: int | None = None
    choices: set[str] | None = None
    max_chars: int | None = None


@dataclass(frozen=True)
class _WarningDraft:
    field_name: str
    raw_value: Any
    default_value: Any
    reason: str


@dataclass(frozen=True)
class _ChoiceCoercion:
    field_name: str
    raw_value: Any
    default: str
    choices: set[str]
    warnings: list[MemoryConfigWarning]


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
    _FieldSpec("memory_compact_auto_trigger_percent", "compact_trigger_percent"),
    _FieldSpec("memory_curator_enabled", "bool"),
    _FieldSpec(
        "memory_curator_provider",
        "choice",
        choices={"auto", "echo", "openai_compatible", "anthropic_compatible"},
    ),
    _FieldSpec("memory_curator_model", "string", max_chars=200),
    _FieldSpec("memory_curator_interval_seconds", "int", 60, 604_800),
    _FieldSpec("memory_curator_turn_threshold", "int", 1, 1_000),
    _FieldSpec("memory_curator_batch_message_limit", "int", 1, 500),
    _FieldSpec("memory_curator_max_input_chars", "int", 2_000, 500_000),
    _FieldSpec("memory_curator_timeout_seconds", "int", 5, 900),
    _FieldSpec("memory_curator_max_retries", "int", 0, 5),
    _FieldSpec("memory_curator_daily_finalize_hour", "int", 0, 23),
    _FieldSpec(
        "memory_curator_auto_promotion_policy",
        "choice",
        choices={"conservative_v1", "manual_only"},
    ),
    _FieldSpec("memory_lesson_min_occurrences", "int", 2, 100),
    _FieldSpec("memory_hot_min_occurrences", "int", 3, 100),
)


# LLM: All MemorySettings fields must be produced in _FIELDS order so warning order and defaults stay deterministic.
# 函数用途: 从配置源构造完整、已验证的 Memory 设置字典。
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


# LLM: Dispatch is based only on typed FieldSpec.kind; unknown prose or runtime values cannot select another parser.
# 函数用途: 按字段规格校验并规范化一个 Memory 配置值。
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
        return _coerce_choice(_ChoiceCoercion(spec.field_name, raw_value, default, spec.choices or set(), warnings))
    if spec.kind == "string":
        return _coerce_string(
            spec.field_name,
            raw_value,
            default=default,
            max_chars=spec.max_chars or 200,
            warnings=warnings,
        )
    if spec.kind == "compact_trigger_percent":
        return _coerce_compact_trigger_percent(spec.field_name, raw_value, default=default, warnings=warnings)
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


# LLM: Model/provider strings must be short single-line values; booleans/numbers cannot be coerced into identifiers.
# 函数用途: 校验后台模型等短字符串配置，非法值回退默认并记录警告。
def _coerce_string(
    field_name: str,
    raw_value: Any,
    *,
    default: str,
    max_chars: int,
    warnings: list[MemoryConfigWarning],
) -> str:
    """Parse a short single-line string without treating booleans/numbers as model names."""
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        if len(normalized) <= max_chars and "\n" not in normalized and "\r" not in normalized:
            return normalized
    _warn(
        warnings,
        _WarningDraft(field_name, raw_value, default, "expected a short single-line string"),
    )
    return default


def _coerce_compact_trigger_percent(
    field_name: str,
    raw_value: Any,
    *,
    default: int,
    warnings: list[MemoryConfigWarning],
) -> int:
    if raw_value is _MISSING:
        return default
    number = _memory_int_number(raw_value)
    if number is None:
        reason = "expected an integer, not a boolean" if isinstance(raw_value, bool) else "expected an integer"
        _warn(warnings, _WarningDraft(field_name, raw_value, default, reason))
        return default
    if number <= 0:
        return 100
    if number < 50:
        _warn(warnings, _WarningDraft(field_name, raw_value, 50, "expected 0 or value between 50 and 100"))
        return 50
    if number > 100:
        _warn(warnings, _WarningDraft(field_name, raw_value, 100, "expected value <= 100"))
        return 100
    return number


def _lookup(source: Mapping[str, Any] | object, field_name: str) -> Any:
    """Read a raw config field from a mapping or dataclass-like object."""
    if isinstance(source, Mapping):
        return source.get(field_name, _MISSING)
    return getattr(source, field_name, _MISSING)


def _warn(
    warnings: list[MemoryConfigWarning],
    draft: _WarningDraft,
) -> None:
    """Append one structured default warning."""
    warnings.append(
        MemoryConfigWarning(
            field_name=draft.field_name,
            raw_value=draft.raw_value,
            default_value=draft.default_value,
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


def _coerce_choice(params: _ChoiceCoercion) -> str:
    """Parse an enum-like string config value against an allowlist."""
    if params.raw_value is _MISSING:
        return params.default
    if isinstance(params.raw_value, str):
        normalized = params.raw_value.strip().lower()
        if normalized in params.choices:
            return normalized
    _warn(
        params.warnings,
        _WarningDraft(params.field_name, params.raw_value, params.default, f"expected one of {sorted(params.choices)}"),
    )
    return params.default


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
