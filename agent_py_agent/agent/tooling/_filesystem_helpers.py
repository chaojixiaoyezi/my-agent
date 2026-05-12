
# LLM: 这里守住控制字符、长度和默认值边界，改动会影响所有文件工具。
# 模块用途: 文件工具的参数解析、路径文本校验和安全读取 helper。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_PATH_CHARS = 4096
_MAX_SEARCH_QUERY_CHARS = 4000
_MAX_SEARCH_LINE_CHARS = 500
_MAX_WRITE_TEXT_CHARS = 1_000_000


# LLM: TextParamOptions 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 文本参数约束模型，集中保存必填、长度和空值规则。
@dataclass(frozen=True)
class TextParamOptions:
    name: str = "value"
    max_chars: int = _MAX_WRITE_TEXT_CHARS
    allow_empty: bool = False
    strip: bool = False


# LLM: _has_control_chars 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 判断 has_control_chars 是否满足安全或状态条件。
def _has_control_chars(text: str) -> bool:
    return any(ord(char) < 32 for char in text)


# LLM: _required_path 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 required_path 步骤，并保持调用方依赖的数据形状。
def _required_path(value: Any, *, name: str = "path") -> str:
    if value is None:
        raise ValueError(f"缺少必填参数 {name}")
    if not isinstance(value, (str, Path)):
        raise ValueError(f"{name} 参数必须是字符串路径")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} 不能为空")
    if len(text) > _MAX_PATH_CHARS:
        raise ValueError(f"{name} 过长，最多 {_MAX_PATH_CHARS} 个字符")
    if _has_control_chars(text):
        raise ValueError(f"{name} 包含不支持的控制字符")
    return text


# LLM: _optional_path 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 optional_path 步骤，并保持调用方依赖的数据形状。
def _optional_path(value: Any, *, default: str = ".") -> str:
    if value is None:
        return default
    return _required_path(value)


# LLM: _text_param 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 text_param 步骤，并保持调用方依赖的数据形状。
def _text_param(
    value: Any,
    *,
    options: TextParamOptions | None = None,
    name: str = "value",
    max_chars: int = _MAX_WRITE_TEXT_CHARS,
    allow_empty: bool = False,
    strip: bool = False,
) -> str:
    values = options or TextParamOptions(name, max_chars, allow_empty, strip)
    name = str(values.name)
    max_chars = int(values.max_chars)
    allow_empty = bool(values.allow_empty)
    strip = bool(values.strip)
    if value is None:
        raise ValueError(f"缺少必填参数 {name}")
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{name} 参数必须是字符串或标量文本")
    text = str(value)
    if strip:
        text = text.strip()
    if not allow_empty and text == "":
        raise ValueError(f"{name} 不能为空")
    if len(text) > max_chars:
        raise ValueError(f"{name} 过长，最多 {max_chars} 个字符")
    return text


# LLM: _int_param 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 int_param 步骤，并保持调用方依赖的数据形状。
def _int_param(value: Any, *, name: str, default: int, min_value: int | None = None) -> int:
    if value is None:
        parsed = default
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} 必须是整数") from exc
    if min_value is not None and parsed < min_value:
        raise ValueError(f"{name} 不能小于 {min_value}")
    return parsed


# LLM: _bool_param 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 bool_param 步骤，并保持调用方依赖的数据形状。
def _bool_param(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return default


# LLM: _bundled_filesystem_param accepts both flat and filesystem-bundled tool parameters during migration.
# 函数用途: 让文件工具兼容 {"path": "..."} 和 {"filesystem": {"path": "..."}} 两种参数形态。
def _bundled_filesystem_param(params: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in params:
        return params.get(key)
    filesystem = params.get("filesystem")
    if isinstance(filesystem, dict) and key in filesystem:
        return filesystem.get(key)
    return default


# LLM: _normalized_workspace_roots resolves and deduplicates allowed filesystem roots.
# 函数用途: 解析并去重工作区根目录，保留第一个主工作区。
def _normalized_workspace_roots(primary: Path, roots: list[Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


# LLM: _is_under_any_root checks containment without following model-provided glob semantics.
# 函数用途: 判断某个路径是否位于任一允许根目录之下。
def _is_under_any_root(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


# LLM: _read_text_safe 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 读取 read_text_safe 数据并转换成内部对象。
def _read_text_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


# LLM: _parse_count_param 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 解析 parse_count_param 数据结构。
def _parse_count_param(value: Any) -> int:
    if value is None:
        return 1
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1
