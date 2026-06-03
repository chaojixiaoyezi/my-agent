

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

_MAX_PATH_CHARS = 4096
_MAX_SEARCH_QUERY_CHARS = 4000
_MAX_SEARCH_LINE_CHARS = 500
_MAX_WRITE_TEXT_CHARS = 1_000_000


@dataclass(frozen=True)
class TextParamOptions:
    name: str = "value"
    max_chars: int = _MAX_WRITE_TEXT_CHARS
    allow_empty: bool = False
    strip: bool = False


def _has_control_chars(text: str) -> bool:
    return any(ord(char) < 32 for char in text)


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


def _optional_path(value: Any, *, default: str = ".") -> str:
    if value is None:
        return default
    return _required_path(value)


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


def _bundled_filesystem_param(params: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in params:
        return params.get(key)
    filesystem = params.get("filesystem")
    if isinstance(filesystem, dict) and key in filesystem:
        return filesystem.get(key)
    return default


def _normalized_workspace_roots(primary: Path, roots: list[Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


def _is_under_any_root(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _read_text_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _parse_count_param(value: Any) -> int:
    if value is None:
        return 1
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1
