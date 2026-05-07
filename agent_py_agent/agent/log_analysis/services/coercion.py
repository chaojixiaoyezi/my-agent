# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

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


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 ConfigWarningInput 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ConfigWarningInput 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class ConfigWarningInput:
    field_name: str
    raw_value: Any
    fallback_value: Any
    reason: str


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 ChoiceCoercionOptions 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ChoiceCoercionOptions 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class ChoiceCoercionOptions:
    default: str
    choices: set[str]
    uppercase: bool = False


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 IntCoercionOptions 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 IntCoercionOptions 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class IntCoercionOptions:
    default: int
    min_value: int
    max_value: int | None


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 lookup 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 lookup 在当前模块中的核心转换或协调步骤，衔接 日志分析模块围绕事件、查询、案例和报告传递结构化事实。
def lookup(source: dict[str, Any] | object, field_name: str) -> Any:
    """Read a config field from a Mapping or plain object, returning _MISSING sentinel if absent."""
    if isinstance(source, dict):
        return source.get(field_name, _MISSING)
    return getattr(source, field_name, _MISSING)


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 append_warning 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append warning 相关记录，集中处理目标路径、格式化和状态更新。
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
            fallback_value=item.fallback_value,
            reason=item.reason,
        )
    )


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 coerce_bool 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce bool 涉及的字段，让后续匹配和存储使用同一形态。
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


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 coerce_choice 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce choice 涉及的字段，让后续匹配和存储使用同一形态。
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


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 coerce_int 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce int 涉及的字段，让后续匹配和存储使用同一形态。
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


# LLM: 日志分析模块围绕事件、查询、案例和报告传递结构化事实；修改 coerce_path_string 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 coerce path string 涉及的字段，让后续匹配和存储使用同一形态。
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
