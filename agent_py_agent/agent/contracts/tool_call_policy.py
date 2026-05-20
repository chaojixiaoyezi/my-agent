# LLM: Tool call policy validates registry intent from machine fields before execution.
# 模块用途: 校验工具是否存在、是否授权、参数是否满足结构化 schema，以及参数是否命中配置化阻断模式。

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .tool_protocol_v2 import normalize_tool_call


# LLM: ToolCallPolicy is a compact machine-readable manifest slice for one execution context.
# 类用途: 保存可用工具、当前允许/禁止工具、必填参数、参数类型和通用参数阻断模式。
@dataclass(frozen=True)
class ToolCallPolicy:
    available_tools: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    required_parameters: dict[str, tuple[str, ...]] = field(default_factory=dict)
    parameter_types: dict[str, dict[str, str]] = field(default_factory=dict)
    blocked_argument_patterns: tuple[str, ...] = ()


# LLM: ToolCallPolicyDecision is the structured result consumed by runners and replay tests.
# 类用途: 返回工具调用是否允许、工具名、错误码和具体机器发现项。
@dataclass(frozen=True)
class ToolCallPolicyDecision:
    ok: bool
    tool_name: str
    error_code: str = ""
    findings: tuple[str, ...] = ()


# LLM: validate_tool_call_policy checks one tool call without executing tools or reading prompt prose.
# 函数用途: 按 policy 校验工具名、授权、必填参数、参数类型和参数阻断模式。
def validate_tool_call_policy(payload: Any, policy: ToolCallPolicy) -> ToolCallPolicyDecision:
    call = normalize_tool_call(payload)
    tool_name = call.tool_name
    if not tool_name:
        return _decision(False, tool_name, "TOOL_NAME_REQUIRED")
    available = _name_set(policy.available_tools)
    if available and tool_name not in available:
        return _decision(False, tool_name, "TOOL_NOT_FOUND")
    if tool_name in _name_set(policy.denied_tools):
        return _decision(False, tool_name, "TOOL_DENIED")
    allowed = _name_set(policy.allowed_tools)
    if allowed and tool_name not in allowed:
        return _decision(False, tool_name, "TOOL_NOT_ALLOWED")

    missing = _missing_required_parameters(tool_name, call.input, policy.required_parameters)
    if missing:
        return _decision(False, tool_name, "TOOL_PARAMETER_REQUIRED", missing)
    type_errors = _parameter_type_errors(tool_name, call.input, policy.parameter_types)
    if type_errors:
        return _decision(False, tool_name, "TOOL_PARAMETER_TYPE_INVALID", type_errors)
    blocked = _blocked_argument_matches(call.input, policy.blocked_argument_patterns)
    if blocked:
        return _decision(False, tool_name, "TOOL_PARAMETER_BLOCKED", blocked)
    return _decision(True, tool_name, "")


# LLM: _missing_required_parameters reads required fields from policy only.
# 函数用途: 返回当前工具缺失的必填参数名，不从工具描述或自然语言里推断。
def _missing_required_parameters(
    tool_name: str,
    params: dict[str, Any],
    required: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    names = required.get(tool_name, ())
    return tuple(name for name in names if name not in params or params.get(name) is None)


# LLM: _parameter_type_errors compares JSON-like params with simple schema type names.
# 函数用途: 返回类型不匹配的字段，格式为 name:expected_type。
def _parameter_type_errors(
    tool_name: str,
    params: dict[str, Any],
    schemas: dict[str, dict[str, str]],
) -> tuple[str, ...]:
    errors: list[str] = []
    for name, expected in schemas.get(tool_name, {}).items():
        if name in params and not _matches_type(params[name], expected):
            errors.append(f"{name}:{expected}")
    return tuple(errors)


# LLM: _blocked_argument_matches scans structured string args against configured patterns.
# 函数用途: 返回命中阻断模式的参数路径，避免把危险命令形态交给工具执行。
def _blocked_argument_matches(params: dict[str, Any], patterns: tuple[str, ...]) -> tuple[str, ...]:
    if not patterns:
        return ()
    findings: list[str] = []
    for path, value in _string_values(params):
        if any(re.search(pattern, value) for pattern in patterns):
            findings.append(path)
    return tuple(findings)


# LLM: _string_values flattens JSON-like args while preserving field paths.
# 函数用途: 遍历结构化参数里的字符串值，供阻断模式检查使用。
def _string_values(value: Any, prefix: str = "") -> tuple[tuple[str, str], ...]:
    if isinstance(value, str):
        return ((prefix or "$", value),)
    if isinstance(value, dict):
        rows: list[tuple[str, str]] = []
        for key, item in value.items():
            rows.extend(_string_values(item, f"{prefix}.{key}" if prefix else str(key)))
        return tuple(rows)
    if isinstance(value, list):
        rows = []
        for index, item in enumerate(value):
            rows.extend(_string_values(item, f"{prefix}[{index}]"))
        return tuple(rows)
    return ()


# LLM: _matches_type implements the small JSON type vocabulary used by offline tool tests.
# 函数用途: 校验 string/integer/number/boolean/object/array/any 类型名。
def _matches_type(value: Any, expected: str) -> bool:
    kind = str(expected or "any").strip().lower()
    if kind in {"", "any"}:
        return True
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    return False


# LLM: _name_set normalizes tool-name policy lists without fuzzy matching.
# 函数用途: 生成去空白的工具名集合，保持大小写敏感的精确匹配。
def _name_set(names: tuple[str, ...]) -> set[str]:
    return {str(item).strip() for item in names if str(item).strip()}


# LLM: _decision keeps policy result construction stable and short.
# 函数用途: 统一创建 ToolCallPolicyDecision，确保 findings 永远是 tuple。
def _decision(
    ok: bool,
    tool_name: str,
    error_code: str,
    findings: tuple[str, ...] = (),
) -> ToolCallPolicyDecision:
    return ToolCallPolicyDecision(ok=ok, tool_name=tool_name, error_code=error_code, findings=findings)


__all__ = ["ToolCallPolicy", "ToolCallPolicyDecision", "validate_tool_call_policy"]
