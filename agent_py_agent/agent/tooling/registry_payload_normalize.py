
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..settings.defaults import default_agent_config
from .json_repair import load_tool_block_json


@dataclass(frozen=True)
class ToolPayloadNormalizeLimits:
    max_fields: int
    max_field_name_chars: int
    max_tool_name_chars: int
    max_parse_error_raw_chars: int


def tool_payload_limits_from_config(config: object | None) -> ToolPayloadNormalizeLimits:
    if config is None:
        config = default_agent_config()
    defaults = _default_tool_payload_limits()
    return ToolPayloadNormalizeLimits(
        max_fields=_config_int(config, "tool_payload_max_fields", defaults.max_fields),
        max_field_name_chars=_config_int(
            config,
            "tool_payload_max_field_name_chars",
            defaults.max_field_name_chars,
        ),
        max_tool_name_chars=_config_int(config, "tool_payload_max_name_chars", defaults.max_tool_name_chars),
        max_parse_error_raw_chars=_config_int(
            config,
            "tool_payload_parse_error_raw_chars",
            defaults.max_parse_error_raw_chars,
        ),
    )


def _default_tool_payload_limits() -> ToolPayloadNormalizeLimits:
    defaults = default_agent_config()
    return ToolPayloadNormalizeLimits(
        max_fields=defaults.tool_payload_max_fields,
        max_field_name_chars=defaults.tool_payload_max_field_name_chars,
        max_tool_name_chars=defaults.tool_payload_max_name_chars,
        max_parse_error_raw_chars=defaults.tool_payload_parse_error_raw_chars,
    )
def parse_tool_block_payload(
    raw: str,
    *,
    limits: ToolPayloadNormalizeLimits | None = None,
) -> dict[str, Any]:
    active_limits = limits or _default_tool_payload_limits()
    try:
        payload = load_tool_block_json(raw)
    except json.JSONDecodeError as exc:
        return parse_error_payload(
            f"工具调用 JSON 解析失败: {exc}",
            raw,
            limits=active_limits,
            error_code="TOOL_CALL_JSON_INVALID",
        )
    if not isinstance(payload, dict):
        return parse_error_payload(
            "工具调用必须是 JSON 对象",
            raw,
            limits=active_limits,
            error_code="TOOL_CALL_JSON_NOT_OBJECT",
        )
    normalized, error = normalize_tool_payload(payload, limits=active_limits)
    if error or normalized is None:
        return parse_error_payload(
            error or "工具调用解析失败",
            raw,
            limits=active_limits,
            error_code="TOOL_CALL_PAYLOAD_INVALID",
        )
    return normalized


def normalize_tool_payload(
    payload: object,
    *,
    limits: ToolPayloadNormalizeLimits | None = None,
) -> tuple[dict[str, Any] | None, str]:
    active_limits = limits or _default_tool_payload_limits()
    if not isinstance(payload, dict):
        return None, "工具调用必须是 JSON 对象"
    normalized, error = _normalize_payload_mapping(payload, active_limits)
    if error:
        return None, error
    if len(normalized) > active_limits.max_fields:
        return None, f"工具调用字段过多，最多 {active_limits.max_fields} 个字段"
    return normalized, ""


def parse_error_payload(
    error: str,
    raw: str,
    *,
    limits: ToolPayloadNormalizeLimits | None = None,
    error_code: str = "TOOL_CALL_PARSE_ERROR",
) -> dict[str, str]:
    active_limits = limits or _default_tool_payload_limits()
    return {
        "tool": "__parse_error__",
        "error_code": error_code,
        "error": error,
        "raw": _truncate(raw, active_limits.max_parse_error_raw_chars),
    }


def tool_name(value: object, *, limits: ToolPayloadNormalizeLimits | None = None) -> str:
    active_limits = limits or _default_tool_payload_limits()
    if value is None:
        raise ValueError("工具调用缺少 tool 字段")
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError("tool 字段必须是字符串工具名")
    name = str(value).strip()
    if not name:
        raise ValueError("工具调用缺少 tool 字段")
    if len(name) > active_limits.max_tool_name_chars:
        raise ValueError(f"tool 字段过长，最多 {active_limits.max_tool_name_chars} 个字符")
    if any(ord(char) < 32 for char in name):
        raise ValueError("tool 字段包含不支持的控制字符")
    return name


def _normalize_payload_mapping(
    payload: dict[Any, Any],
    limits: ToolPayloadNormalizeLimits,
) -> tuple[dict[str, Any], str]:
    if len(payload) > limits.max_fields:
        return {}, f"工具调用字段过多，最多 {limits.max_fields} 个字段"

    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        key_text = str(key)
        if not key_text:
            return {}, "工具调用包含空参数名"
        if len(key_text) > limits.max_field_name_chars:
            return {}, f"工具调用参数名过长，最多 {limits.max_field_name_chars} 个字符"
        if any(ord(char) < 32 for char in key_text):
            return {}, "工具调用参数名包含不支持的控制字符"
        normalized[key_text] = value
    return normalized, ""


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... 已截断"


def _config_int(config: object, key: str, default: int) -> int:
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return max(0, int(default))
