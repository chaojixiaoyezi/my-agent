# LLM: YAML-lite parsing stays separate from AgentConfig so the config model file stays small.
# 模块用途: 解析项目自带的简化 YAML 配置文件和值。

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


# LLM: parse_scalar 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 解析简化 YAML 中的单个标量、行内列表或行内字典值。
def parse_scalar(value: str) -> Any:
    value = value.strip().strip('"').strip("'")
    if value.startswith("[") and value.endswith("]"):
        parsed = _parse_inline_list(value)
        if parsed is not None:
            return parsed
    if value.startswith("{") and value.endswith("}"):
        parsed = _parse_inline_dict(value)
        if parsed is not None:
            return parsed
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


# LLM: _parse_inline_list 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 解析简化 YAML 的行内列表，解析失败时返回 None 让调用方回退。
def _parse_inline_list(value: str) -> list[Any] | None:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    return parsed


# LLM: _parse_inline_dict lets config expose small maps without adding a full YAML dependency.
# 函数用途: 解析简化 YAML 的行内字典，例如 {"worker": 8, "root": "off"}；解析失败时回退普通字符串。
def _parse_inline_dict(value: str) -> dict[str, Any] | None:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): item for key, item in parsed.items()}


# LLM: load_simple_yaml 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 读取简化 YAML 配置文件并转换成字典。
def load_simple_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key: str | None = None
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if _append_yaml_list_item(data, current_key, line):
            continue
        current_key = _handle_yaml_mapping_line(data, current_key, line)
    return data


# LLM: _append_yaml_list_item 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 处理简化 YAML 的列表项行，并在成功处理时返回 True。
def _append_yaml_list_item(data: dict[str, Any], current_key: str | None, line: str) -> bool:
    if not (line.startswith("  - ") and current_key):
        return False
    data.setdefault(current_key, []).append(parse_scalar(line[4:]))
    return True


# LLM: _handle_yaml_mapping_line 属于 配置系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 处理简化 YAML 的 key/value 行，并返回当前列表 key。
def _handle_yaml_mapping_line(data: dict[str, Any], current_key: str | None, line: str) -> str | None:
    if ":" not in line or line.startswith(" "):
        return current_key
    key, value = line.split(":", 1)
    key = key.strip()
    value = value.strip()
    if value == "":
        data[key] = []
        return key
    data[key] = parse_scalar(value)
    return None
