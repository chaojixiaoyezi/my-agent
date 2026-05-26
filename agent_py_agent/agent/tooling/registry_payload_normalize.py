# LLM: Tool payload normalization repairs common model JSON mistakes before auth/execution.
# 模块用途: 解析工具调用 JSON、归一工具名和参数别名，并生成稳定的错误载荷。

from __future__ import annotations

import json
from typing import Any

from .json_repair import load_tool_block_json

MAX_TOOL_PAYLOAD_FIELDS = 64
MAX_TOOL_FIELD_NAME_CHARS = 128
MAX_TOOL_NAME_CHARS = 128
MAX_PARSE_ERROR_RAW_CHARS = 1000
MODEL_WRAPPER_PARAM_KEYS = {
    "args",
    "actual_parameter_name",
    "api",
    "arguments",
    "filesystem",
    "log_analysis",
    "memory",
    "orchestration",
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
    "fetch": "web_fetch",
    "fetch_url": "web_fetch",
    "web_fetch": "web_fetch",
    "web_extract": "web_extract",
    "grep": "search_text",
    "http": "http_request",
    "list": "list_files",
    "ls": "list_files",
    "open": "read_file",
    "patch": "apply_patch",
    "read": "read_file",
    "request": "http_request",
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


# LLM: parse_tool_block_payload is the tolerant bridge from text protocol to dict payload.
# 函数用途: 解析单个工具调用 JSON 块；失败时返回 __parse_error__ 载荷供上层统一处理。
def parse_tool_block_payload(raw: str) -> dict[str, Any]:
    try:
        payload = load_tool_block_json(raw)
    except json.JSONDecodeError as exc:
        return parse_error_payload(f"工具调用 JSON 解析失败: {exc}", raw)
    if not isinstance(payload, dict):
        return parse_error_payload("工具调用必须是 JSON 对象", raw)
    normalized, error = normalize_tool_payload(payload)
    if error or normalized is None:
        return parse_error_payload(error or "工具调用解析失败", raw)
    return normalized


# LLM: normalize_tool_payload validates and canonicalizes payload shape before execution.
# 函数用途: 把输入值归一成工具系统内部使用的稳定格式，兼容常见工具名和参数别名。
def normalize_tool_payload(payload: object) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(payload, dict):
        return None, "工具调用必须是 JSON 对象"
    normalized, error = _normalize_payload_mapping(payload)
    if error:
        return None, error
    expanded, error = _unwrap_param_name_bundle(normalized)
    if error:
        return None, error
    canonical, error = _canonicalize_tool_payload(expanded)
    if error:
        return None, error
    if len(canonical) > MAX_TOOL_PAYLOAD_FIELDS:
        return None, f"工具调用字段过多，最多 {MAX_TOOL_PAYLOAD_FIELDS} 个字段"
    return canonical, ""


# LLM: parse_error_payload preserves enough raw text for debugging without flooding context.
# 函数用途: 生成工具解析错误载荷，并截断原始内容。
def parse_error_payload(error: str, raw: str) -> dict[str, str]:
    return {
        "tool": "__parse_error__",
        "error": error,
        "raw": _truncate(raw, MAX_PARSE_ERROR_RAW_CHARS),
    }


# LLM: tool_name extracts and validates the canonical tool identifier.
# 函数用途: 把工具名字段转成安全字符串；缺失、空值、控制字符会抛出 ValueError。
def tool_name(value: object) -> str:
    if value is None:
        raise ValueError("工具调用缺少 tool 字段")
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError("tool 字段必须是字符串工具名")
    name = str(value).strip()
    if not name:
        raise ValueError("工具调用缺少 tool 字段")
    if len(name) > MAX_TOOL_NAME_CHARS:
        raise ValueError(f"tool 字段过长，最多 {MAX_TOOL_NAME_CHARS} 个字符")
    if any(ord(char) < 32 for char in name):
        raise ValueError("tool 字段包含不支持的控制字符")
    return name


# LLM: _normalize_payload_mapping validates a tool payload map before dispatch.
# 函数用途: 检查工具参数名是否安全，并把参数键统一转成字符串，避免坏键污染执行层。
def _normalize_payload_mapping(payload: dict[Any, Any]) -> tuple[dict[str, Any], str]:
    if len(payload) > MAX_TOOL_PAYLOAD_FIELDS:
        return {}, f"工具调用字段过多，最多 {MAX_TOOL_PAYLOAD_FIELDS} 个字段"

    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        key_text = str(key)
        if not key_text:
            return {}, "工具调用包含空参数名"
        if len(key_text) > MAX_TOOL_FIELD_NAME_CHARS:
            return {}, f"工具调用参数名过长，最多 {MAX_TOOL_FIELD_NAME_CHARS} 个字符"
        if any(ord(char) < 32 for char in key_text):
            return {}, "工具调用参数名包含不支持的控制字符"
        normalized[key_text] = value
    return normalized, ""


# LLM: _unwrap_param_name_bundle repairs a common model mistake without hiding collisions.
# 函数用途: 当模型把真实参数误包进 param_name/arguments 字段时，将其展开成工具可执行的扁平参数。
def _unwrap_param_name_bundle(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    wrapper_keys = [key for key in payload if key != "tool"]
    if len(wrapper_keys) != 1 or wrapper_keys[0] not in MODEL_WRAPPER_PARAM_KEYS:
        return payload, ""
    wrapper_key = wrapper_keys[0]
    wrapper_value = payload.get(wrapper_key)
    if isinstance(wrapper_value, str):
        wrapper_value, error = _parse_wrapper_param_json(wrapper_key, wrapper_value)
        if error:
            return {}, error
    if not isinstance(wrapper_value, dict):
        return payload, ""
    bundled, error = _normalize_payload_mapping(wrapper_value)
    if error:
        return {}, error
    if "tool" in bundled:
        return {}, f"{wrapper_key} 参数包不能包含 tool 字段"
    return {"tool": payload["tool"], **bundled}, ""


# LLM: _parse_wrapper_param_json accepts structured JSON-string wrappers without guessing semantics.
# 函数用途: 把 arguments/params 中的 JSON 字符串解成对象；只做格式修复，不读取自然语言描述。
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


# LLM: _canonicalize_tool_payload repairs stable aliases before auth and execution.
# 函数用途: 把 JSON 工具调用里的 write/read/file_path 等常见别名归一，避免模型小错直接卡住。
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


# LLM: _canonical_tool_name keeps parser and direct execution tolerant of simple aliases.
# 函数用途: 统一 JSON 工具名别名；未知工具名保留给后续鉴权/未知工具错误处理。
def _canonical_tool_name(value: object) -> object:
    if not isinstance(value, str):
        return value
    name = value.strip()
    return TOOL_NAME_ALIASES.get(name, TOOL_NAME_ALIASES.get(name.lower(), name))


# LLM: _truncate bounds raw parse-error previews for prompt safety.
# 函数用途: 截断过长文本，并追加清晰的截断提示。
def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... 已截断"
