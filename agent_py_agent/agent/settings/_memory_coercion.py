# LLM: 所有记忆字段共用此类型表；模型引用只保留编号，存在、启用与用途由原模型目录解析器校验，不拼接聊天连接。
# 模块用途: 校验 Memory/Curator 配置类型、枚举与范围，并生成不含秘密的结构化警告。
"""Coercion helpers for memory-related config fields."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ._memory_types import MemoryConfigWarning, MemorySettings

_MISSING = object()
_INT_PATTERN = re.compile(r"-?[0-9]+")
# 压缩点与压缩后恢复目标的有效范围：配置解析与 user_config 自助校验共用，并与运行时夹取一致（测试钉住），
# 保证自助修改接受的值一定原样生效，不会再被运行时夹到别的值。
COMPACT_TRIGGER_PERCENT_RANGE = (50, 100)
COMPACT_RECOVERY_PERCENT_RANGE = (25, 80)


# LLM: 字段规格是唯一类型转换规则；max_chars 只限制供应商及模型身份长度，不解析其语义。
# 类用途: 描述一个 Memory 配置字段的类型、范围、枚举或长度限制。
@dataclass(frozen=True)
class _FieldSpec:
    field_name: str
    kind: str
    min_value: int | None = None
    max_value: int | None = None
    choices: set[str] | None = None
    max_chars: int | None = None


# LLM: 记忆配置解析叶；严格类型及降级告警沿原规则，不凭文字猜开关，联测Memory配置。
# 类用途: 暂存一次配置降级告警的键、原值与原因。
@dataclass(frozen=True)
class _WarningDraft:
    field_name: str
    raw_value: Any
    default_value: Any
    reason: str


# LLM: 记忆配置解析叶；严格类型及降级告警沿原规则，不凭文字猜开关，联测Memory配置。
# 类用途: 描述枚举配置的允许值和默认值，供统一解析。
@dataclass(frozen=True)
class _ChoiceCoercion:
    field_name: str
    raw_value: Any
    default: str
    choices: set[str]
    warnings: list[MemoryConfigWarning]


# LLM: 记忆配置解析叶；严格类型及降级告警沿原规则，不凭文字猜开关，联测Memory配置。
# 类用途: 描述整数配置的范围、默认值与来源信息。
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
    _FieldSpec("memory_rule_routing_mode", "choice", choices={"off", "soft", "strict"}),
    _FieldSpec("memory_rule_auto_read_limit", "int", 0, None),
    _FieldSpec("memory_resume_auto_context_mode", "choice", choices={"off", "trigger", "always"}),
    _FieldSpec("memory_compact_trigger_percent", "clamped_int", 50, 95),
    _FieldSpec("force_compact_trigger_percent", "bool"),
    _FieldSpec("memory_compact_idle_trigger_percent", "clamped_int", 0, 95),
    _FieldSpec("memory_compact_idle_seconds", "idle_seconds", 0, None),
    _FieldSpec("memory_compact_idle_max_per_tick", "int", 0, None),
    _FieldSpec("memory_compact_auto_trigger_percent", "compact_trigger_percent"),
    # 0 表示不封顶；负数、非整数回到默认 300000，与 runtime 的 compact_trigger_max_tokens 同一口径。
    _FieldSpec("memory_compact_auto_trigger_max_tokens", "int", 0, None),
    _FieldSpec("memory_compact_recovery_target_percent", "compact_recovery_percent"),
    # 0 表示沿用主请求输出上限；负数、非整数回到默认 16384（与 call_runtime.compact_summary_output_reserve_tokens 同一口径）。
    _FieldSpec("memory_compact_summary_max_output_tokens", "int", 0, None),
    _FieldSpec("memory_compact_remote_enabled", "bool"),
    # 档位字符串在使用处按 normalize_reasoning_level 规范化；非法值等于留空（沿用会话档位）。
    _FieldSpec("memory_compact_reasoning_level", "string", max_chars=16),
    _FieldSpec("memory_curator_enabled", "bool"),
    _FieldSpec("memory_curator_model_profile", "string", max_chars=100),
    _FieldSpec("memory_curator_interval_seconds", "int", 60, 604_800),
    _FieldSpec("memory_curator_turn_threshold", "int", 1, 1_000),
    _FieldSpec("memory_curator_max_input_chars", "int", 2_000, 500_000),
    _FieldSpec("memory_curator_timeout_seconds", "int", 5, 900),
    _FieldSpec("memory_curator_daily_finalize_hour", "int", 0, 23),
    _FieldSpec(
        "memory_curator_auto_promotion_policy",
        "choice",
        choices={"conservative_v1", "manual_only"},
    ),
    _FieldSpec("memory_lesson_min_occurrences", "int", 2, 100),
    _FieldSpec("memory_hot_min_occurrences", "int", 3, 100),
)


# LLM: 原warning及决策校验保持；管理员强制别名在配置源规范化，不进入用户覆盖可写字段。
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
    for key in ("admin_force_compact_trigger_percent", "memory_compact_admin_forced"):
        raw = _lookup(source, key)
        if raw is not _MISSING:
            result["force_compact_trigger_percent"] |= _coerce_bool(key, raw, default=False, warnings=warnings)
    from .decision_settings_defaults import decision_config_fields, validate_config_decision_fields

    decision = {name: _lookup(source, name) for domain, name in decision_config_fields().values() if domain == "memory"}
    decision = {key: value for key, value in decision.items() if value is not _MISSING}
    result.update(validate_config_decision_fields(decision, domain="memory"))
    return result


# LLM: 只按显式字段规格解析；新比例严格整数夹取，闲置秒数0关闭、启用至少60，不改变旧字段校验。
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
    if spec.kind in {"clamped_int", "idle_seconds"}:
        number = _memory_int_number(raw_value)
        if number is None:
            return default
        number = max(spec.min_value or 0, number)
        if spec.kind == "idle_seconds":
            return max(60, number) if number else 0
        return min(spec.max_value, number)
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
    if spec.kind == "compact_recovery_percent":
        return _coerce_compact_recovery_percent(
            spec.field_name,
            raw_value,
            default=default,
            warnings=warnings,
        )
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


# LLM: 模型及供应商字符串必须是短单行值，不得把布尔值或数字强转成身份标识。
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


# LLM: 与运行时 compact_trigger_percent 同一边界（0 表示 100、低于下限夹到下限、高于上限夹到上限），范围取自
# COMPACT_TRIGGER_PERCENT_RANGE；夹取都留结构化警告。改边界须同步运行时与 user_config 的一致性测试。
# 函数用途: 校验自动压缩触发百分比，越界时限制到有效范围并留下配置警告。
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
    low, high = COMPACT_TRIGGER_PERCENT_RANGE
    if number <= 0:
        return high
    if number < low:
        _warn(warnings, _WarningDraft(field_name, raw_value, low, f"expected 0 or value between {low} and {high}"))
        return low
    if number > high:
        _warn(warnings, _WarningDraft(field_name, raw_value, high, f"expected value <= {high}"))
        return high
    return number


# LLM: 配置解析与运行时压缩边界保持一致，YAML不得产生不同于直接构造AgentConfig的目标；被限幅的值仍须报告明确结构化告警。
# 范围取自 COMPACT_RECOVERY_PERCENT_RANGE，user_config 自助校验用同一常量。
# 函数用途: 校验压缩恢复目标百分比，越界时限制到有效范围（25%--80%）并留下配置警告。
def _coerce_compact_recovery_percent(
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
    low, high = COMPACT_RECOVERY_PERCENT_RANGE
    if number < low:
        _warn(warnings, _WarningDraft(field_name, raw_value, low, f"expected value between {low} and {high}"))
        return low
    if number > high:
        _warn(warnings, _WarningDraft(field_name, raw_value, high, f"expected value between {low} and {high}"))
        return high
    return number


# LLM: 记忆配置解析叶；严格类型及降级告警沿原规则，不凭文字猜开关，联测Memory配置。
# 函数用途: 从映射或配置对象读取原字段，保留缺省与显式值的差别。
def _lookup(source: Mapping[str, Any] | object, field_name: str) -> Any:
    """Read a raw config field from a mapping or dataclass-like object."""
    if isinstance(source, Mapping):
        return source.get(field_name, _MISSING)
    return getattr(source, field_name, _MISSING)


# LLM: 记忆配置解析叶；严格类型及降级告警沿原规则，不凭文字猜开关，联测Memory配置。
# 函数用途: 追加一条结构化默认值告警，不输出敏感配置正文。
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


# LLM: 记忆配置解析叶；严格类型及降级告警沿原规则，不凭文字猜开关，联测Memory配置。
# 函数用途: 按原布尔规则解析配置，含糊或注入式字符串不能变成有效开关。
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


# LLM: 记忆配置解析叶；严格类型及降级告警沿原规则，不凭文字猜开关，联测Memory配置。
# 函数用途: 按有限配置选项解析字符串，非法值记录告警并沿默认。
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


# LLM: 记忆配置解析叶；严格类型及降级告警沿原规则，不凭文字猜开关，联测Memory配置。
# 函数用途: 只按ASCII整数和闭合上下界解析数值配置。
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


# LLM: 记忆配置解析叶；严格类型及降级告警沿原规则，不凭文字猜开关，联测Memory配置。
# 函数用途: 把记忆配置的原始数值转换成受约束整数候选。
def _memory_int_number(raw_value: Any) -> int | None:
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, int):
        return raw_value
    if isinstance(raw_value, str) and _INT_PATTERN.fullmatch(raw_value.strip()):
        return int(raw_value.strip())
    return None
