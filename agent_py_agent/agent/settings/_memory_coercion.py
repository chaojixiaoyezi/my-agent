"""Coercion helpers for memory-related config fields."""

# LLM: 配置值的拒绝条件会影响安全边界和用户提示，调整时同步配置测试。
# 模块用途: 内存配置的严格类型转换与告警生成，避免宽松配置进入运行态。

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ._memory_types import MemoryConfigWarning, MemorySettings

_MISSING = object()
_INT_PATTERN = re.compile(r"-?[0-9]+")


# LLM: _FieldSpec 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 内存配置字段规范，保存字段名、默认值和类型转换策略。
@dataclass(frozen=True)
class _FieldSpec:
    field_name: str
    kind: str
    min_value: int | None = None
    max_value: int | None = None
    choices: set[str] | None = None


# LLM: _WarningDraft 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 配置告警草稿，先收集字段和值再生成用户可见告警。
@dataclass(frozen=True)
class _WarningDraft:
    field_name: str
    raw_value: Any
    fallback_value: Any
    reason: str


# LLM: _ChoiceCoercion 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 枚举配置转换参数，保存允许值和默认选择。
@dataclass(frozen=True)
class _ChoiceCoercion:
    field_name: str
    raw_value: Any
    default: str
    choices: set[str]
    warnings: list[MemoryConfigWarning]


# LLM: _IntCoercion 属于 配置系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 整数配置转换参数，保存默认值和闭区间边界。
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
    _FieldSpec("memory_compact_auto_allow_apply", "bool"),
)


# LLM: _build_memory_settings_dict 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 组装 build_memory_settings_dict 需要的结构化对象或服务实例。
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


# LLM: _coerce_field 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把原始配置值转换成目标类型，失败时回退默认值并记录告警。
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


# LLM: _lookup 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 配置系统 中的 lookup 步骤，并保持调用方依赖的数据形状。
def _lookup(source: Mapping[str, Any] | object, field_name: str) -> Any:
    """Read a raw config field from a mapping or dataclass-like object."""
    if isinstance(source, Mapping):
        return source.get(field_name, _MISSING)
    return getattr(source, field_name, _MISSING)


# LLM: _warn 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 配置系统 中的 warn 步骤，并保持调用方依赖的数据形状。
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


# LLM: _coerce_bool 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把原始配置值转换成目标类型，失败时回退默认值并记录告警。
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


# LLM: _coerce_choice 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把原始配置值转换成目标类型，失败时回退默认值并记录告警。
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


# LLM: _coerce_int 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把原始配置值转换成目标类型，失败时回退默认值并记录告警。
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


# LLM: _memory_int_number 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 配置系统 中的 memory_int_number 步骤，并保持调用方依赖的数据形状。
def _memory_int_number(raw_value: Any) -> int | None:
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, int):
        return raw_value
    if isinstance(raw_value, str) and _INT_PATTERN.fullmatch(raw_value.strip()):
        return int(raw_value.strip())
    return None
