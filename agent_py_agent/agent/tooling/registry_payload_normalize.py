
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
MODEL_WRAPPER_PARAM_KEYS = {
    "args",
    "actual_parameter_name",
    "api",
    "arguments",
    "filesystem",
    "log_analysis",
    "memory",
    "param_name",
    "parameters",
    "params",
    "shell",
    "system",
    "web",
}
TOOL_NAME_ALIASES = {
    "WRITE_FILE_RAW": "write_file",
    "apply_patch": "apply_patch",
    "bash": "run_command",
    "cat": "read_file",
    "command": "run_command",
    "exec": "run_command",
    "web_fetch": "web_fetch",
    "grep": "search_text",
    "list": "list_files",
    "ls": "list_files",
    "open": "read_file",
    "patch": "apply_patch",
    "read": "read_file",
    "search": "search_text",
    "sh": "run_command",
    "shell": "run_command",
    "write": "write_file",
    "write_file_raw": "write_file",
}
FILESYSTEM_PATH_PARAM_ALIASES = {
    "dir": "path",
    "directory": "path",
    "file": "path",
    "file_path": "path",
    "filepath": "path",
    "filename": "path",
    "target": "path",
    "target_path": "path",
}
PARAM_ALIASES_BY_TOOL = {
    "apply_patch": {},
    "list_files": FILESYSTEM_PATH_PARAM_ALIASES,
    "read_file": FILESYSTEM_PATH_PARAM_ALIASES,
    "write_file": FILESYSTEM_PATH_PARAM_ALIASES,
    "search_text": {
        **FILESYSTEM_PATH_PARAM_ALIASES,
        "keyword": "query",
        "pattern": "query",
        "search_text": "query",
        "text": "query",
    },
    "read_artifact": {
        "artifact": "artifact_ref",
        "artifact_path": "artifact_ref",
        "call_id": "artifact_ref",
        "path": "artifact_ref",
        "ref": "artifact_ref",
        "scoped_call_id": "artifact_ref",
        "limit": "max_chars",
        "max_length": "max_chars",
    },
    "run_command": {
        "cmd": "command",
        "command_text": "command",
        "shell": "command",
        "cwd": "working_dir",
        "workdir": "working_dir",
        "working_directory": "working_dir",
        "timeout_seconds": "timeout",
    },
}


def parse_tool_block_payload(
    raw: str,
    *,
    limits: ToolPayloadNormalizeLimits | None = None,
) -> dict[str, Any]:
    active_limits = limits or _default_tool_payload_limits()
    try:
        payload = load_tool_block_json(raw)
    except json.JSONDecodeError as exc:
        return parse_error_payload(f"工具调用 JSON 解析失败: {exc}", raw, limits=active_limits)
    if not isinstance(payload, dict):
        return parse_error_payload("工具调用必须是 JSON 对象", raw, limits=active_limits)
    normalized, error = normalize_tool_payload(payload, limits=active_limits)
    if error or normalized is None:
        return parse_error_payload(error or "工具调用解析失败", raw, limits=active_limits)
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
    expanded, error = _unwrap_param_name_bundle(normalized, active_limits)
    if error:
        return None, error
    canonical, error = _canonicalize_tool_payload(expanded)
    if error:
        return None, error
    if len(canonical) > active_limits.max_fields:
        return None, f"工具调用字段过多，最多 {active_limits.max_fields} 个字段"
    return canonical, ""


def parse_error_payload(
    error: str,
    raw: str,
    *,
    limits: ToolPayloadNormalizeLimits | None = None,
) -> dict[str, str]:
    active_limits = limits or _default_tool_payload_limits()
    return {
        "tool": "__parse_error__",
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


def _unwrap_param_name_bundle(
    payload: dict[str, Any],
    limits: ToolPayloadNormalizeLimits,
) -> tuple[dict[str, Any], str]:
    wrapper_keys = [key for key in payload if key != "tool"]
    tool = str(payload.get("tool") or "").strip()
    if len(wrapper_keys) != 1 or (wrapper_keys[0] not in MODEL_WRAPPER_PARAM_KEYS and wrapper_keys[0] != tool):
        return payload, ""
    wrapper_key = wrapper_keys[0]
    wrapper_value = payload.get(wrapper_key)
    if isinstance(wrapper_value, str):
        wrapper_value, error = _parse_wrapper_param_json(wrapper_key, wrapper_value)
        if error:
            return {}, error
    if not isinstance(wrapper_value, dict):
        return payload, ""
    bundled, error = _normalize_payload_mapping(wrapper_value, limits)
    if error:
        return {}, error
    if "tool" in bundled:
        return {}, f"{wrapper_key} 参数包不能包含 tool 字段"
    return {"tool": payload["tool"], **bundled}, ""


def _parse_wrapper_param_json(wrapper_key: str, raw_value: str) -> tuple[object, str]:
    text = raw_value.strip()
    if not text:
        return raw_value, ""
    if not text.startswith("{"):
        return raw_value, ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{wrapper_key} 参数包 JSON 解析失败: {exc}"
    if not isinstance(parsed, dict):
        return None, f"{wrapper_key} 参数包必须是 JSON 对象"
    return parsed, ""


def _canonicalize_tool_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    tool = _canonical_tool_name(payload.get("tool"))
    normalized: dict[str, Any] = {"tool": tool} if "tool" in payload else {}
    aliases = PARAM_ALIASES_BY_TOOL.get(tool, {})
    for key, value in payload.items():
        if key == "tool":
            continue
        canonical_key = aliases.get(key, key)
        if canonical_key in normalized and normalized[canonical_key] != value:
            return {}, (
                "conflicting parameter aliases: "
                f"{key} conflicts with {canonical_key}; 请只保留一个参数名。"
            )
        normalized[canonical_key] = value
    return normalized, ""


def _canonical_tool_name(value: object) -> object:
    if not isinstance(value, str):
        return value
    name = value.strip()
    return TOOL_NAME_ALIASES.get(name, TOOL_NAME_ALIASES.get(name.lower(), name))


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... 已截断"


def _config_int(config: object, key: str, fallback: int) -> int:
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return max(0, int(fallback))
