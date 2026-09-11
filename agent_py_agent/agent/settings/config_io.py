
from __future__ import annotations

import ast
import threading
from pathlib import Path
from typing import Any

# CPython 3.11 tracks AST-constructor recursion depth in process-global state.
# Concurrent ``ast.literal_eval`` calls can therefore raise the documented
# ``AST constructor recursion depth mismatch`` SystemError.  my-agent still
# supports Python 3.11 on deployed hosts, and scoped Gateway workers may load
# the same configuration in parallel, so serialize only this tiny parse step.
# Python 3.13 fixed the upstream parser race; keeping the lock is harmless on
# newer interpreters and avoids version-dependent request failures.
_AST_LITERAL_EVAL_LOCK = threading.Lock()


def parse_scalar(value: str) -> Any:
    value = value.strip()
    # 引号包裹 = 显式字符串:剥外层引号后原样返回,不做 int/bool/list 推断。
    # YAML 语义:qq_app_id: "1900000000" 是字符串(用户加引号正是为强制字符串),
    # 不能被 int 化——否则纯数字 ID/手机号/账号会被 int 化后又被 string 字段丢成空。
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
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


def _parse_inline_list(value: str) -> list[Any] | None:
    try:
        parsed = _literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    return parsed


def _parse_inline_dict(value: str) -> dict[str, Any] | None:
    try:
        parsed = _literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {str(key): item for key, item in parsed.items()}


def _literal_eval(value: str) -> Any:
    with _AST_LITERAL_EVAL_LOCK:
        return ast.literal_eval(value)


def _yaml_quote_step(ch: str, in_single: bool, in_double: bool) -> tuple[bool, bool, bool]:
    if ch == "'" and not in_double:
        return (not in_single, in_double, False)
    if ch == '"' and not in_single:
        return (in_single, not in_double, False)
    if ch == "#" and not in_single and not in_double:
        return (in_single, in_double, True)  # 引号外的 # = 注释起点
    return (in_single, in_double, False)


def _strip_yaml_comment(line: str) -> str:
    """去行内注释但不动引号内的 '#'(修 color: "#ff0000" / 含 # 的 URL/口令被截成空的 bug,审计 #24)。"""
    in_single = in_double = False
    for i, ch in enumerate(line):
        in_single, in_double, is_comment = _yaml_quote_step(ch, in_single, in_double)
        if is_comment:
            return line[:i].rstrip()
    return line.rstrip()


def load_simple_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key: str | None = None
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = _strip_yaml_comment(raw)  # 引号感知去注释(原 split('#') 会截断引号内的 #)
        if not line.strip():
            continue
        if _append_yaml_list_item(data, current_key, line):
            continue
        current_key = _handle_yaml_mapping_line(data, current_key, line)
    return data


def _append_yaml_list_item(data: dict[str, Any], current_key: str | None, line: str) -> bool:
    # YAML 允许 sequence indicator 与父 mapping key 同级，也允许缩进：
    # ``items:\n- a`` 和 ``items:\n  - a`` 都是合法写法。配置 loader 只支持
    # 顶层 mapping + scalar/list，因此在已有 current_key 时统一按去前导空白后的
    # ``- `` 解析即可；后续新的顶层 mapping 仍由 _handle_yaml_mapping_line 收束。
    stripped = line.lstrip()
    if not (stripped.startswith("- ") and current_key):
        return False
    data.setdefault(current_key, []).append(parse_scalar(stripped[2:]))
    return True


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


def _format_yaml_scalar(value: str) -> str:
    """把字符串值格式化成 mini-yaml 标量:纯整数原样;其余双引号包裹。

    含双/单引号或换行的值会写坏这套"够用版"yaml(parse_scalar 只剥外层引号、不解析转义),
    直接拒绝并让调用方提示手动编辑——宁可不写,也不写出半个坏配置。
    """
    if value != "" and value.lstrip("-").isdigit():
        return value
    if '"' in value or "'" in value or "\n" in value:
        raise ValueError("值含引号或换行,这套简化 yaml 写回不安全,请手动编辑配置文件。")
    return f'"{value}"'


def _replace_top_level_line(lines: list[str], key: str, new_line: str) -> str | None:
    """就地把首个顶层 `key:` 行换成 new_line,返回其旧值文本;没有这行返回 None。

    只认顶层标量行(行首无缩进、非注释、含冒号),不碰缩进行/注释/列表项。
    """
    for i, raw in enumerate(lines):
        if raw.startswith((" ", "\t")) or raw.lstrip().startswith("#") or ":" not in raw:
            continue
        if raw.split(":", 1)[0].strip() == key:
            lines[i] = new_line
            return raw.split(":", 1)[1].strip()
    return None


def set_simple_yaml_value(path: Path, key: str, value: str) -> tuple[str | None, str]:
    """把顶层 key 设为 value,保留注释与其余行(标准库,无 PyYAML)。返回 (旧值文本或 None, 新行)。

    找不到该顶层 key 则在末尾追加。写回走"同目录临时文件 + 原子替换",避免写一半把配置文件弄残。
    """
    new_line = f"{key}: {_format_yaml_scalar(value)}"
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    old = _replace_top_level_line(lines, key, new_line)
    if old is None:
        lines.append(new_line)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return old, new_line
