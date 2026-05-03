"""LLM: 本模块包含配置字段的私有辅助函数：哨兵值、正则、查找、警告和类型转换。

新手说明:
这里包含 _MISSING 哨兵、_lookup、_warn、_coerce_bool、_coerce_choice、_coerce_int、_coerce_path_string。
所有高风险能力在归一化后保持默认关闭，配置写错不会悄悄打开危险功能。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .config_model import LogAnalysisConfigWarning

_MISSING = object()
_INT_PATTERN = re.compile(r"-?[0-9]+")
_LEVELS = {"L0", "L1", "L2", "L3", "L4", "L5"}


def _lookup(source: Mapping[str, Any] | object, field_name: str) -> Any:
    """LLM: 从 Mapping 或普通对象中读取配置字段，缺失时返回 _MISSING 哨兵。

    新手说明:
    测试和调用方可能传字典，也可能传对象。这个函数统一读取方式。

    参数说明:
    source: 配置来源，可能是 dict 或有属性的对象。
    field_name: 要读取的字段名。

    返回说明:
    找到字段则返回原始值；找不到返回 _MISSING，用来区分"没写"和"写了 None"。
    """
    if isinstance(source, Mapping):
        return source.get(field_name, _MISSING)
    return getattr(source, field_name, _MISSING)


def _warn(
    warnings: list[LogAnalysisConfigWarning],
    field_name: str,
    raw_value: Any,
    fallback_value: Any,
    reason: str,
) -> None:
    """LLM: 追加一条配置 warning，记录原始值、回退值和原因。

    新手说明:
    每次发现坏配置，不直接 print，也不吞掉；统一写进 warnings 列表，最后给 doctor/CLI 展示。

    参数说明:
    warnings: 要追加 warning 的列表，会被原地修改。
    field_name: 出问题的字段名。
    raw_value: 用户写的原始值。
    fallback_value: 程序采用的回退值。
    reason: 回退原因。

    返回说明:
    没有返回值；结果追加到 warnings。
    """
    warnings.append(
        LogAnalysisConfigWarning(
            field_name=field_name,
            raw_value=raw_value,
            fallback_value=fallback_value,
            reason=reason,
        )
    )


def _coerce_bool(
    field_name: str,
    raw_value: Any,
    *,
    default: bool,
    warnings: list[LogAnalysisConfigWarning],
) -> bool:
    """LLM: 把用户配置值安全转换成 bool，坏值回退默认值并写 warning。

    新手说明:
    支持 True/False、0/1、"true"/"false"、"yes"/"no"、"on"/"off"。
    其它值会被认为不清楚，回退 default。

    参数说明:
    field_name: 字段名，用于 warning。
    raw_value: 原始值，可能是 _MISSING、bool、int、str 或坏值。
    default: 缺失或坏值时使用的默认值。
    warnings: warning 列表。

    返回说明:
    返回 bool。
    """
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
    _warn(warnings, field_name, raw_value, default, "expected a clear boolean value")
    return default


def _coerce_choice(
    field_name: str,
    raw_value: Any,
    *,
    default: str,
    choices: set[str],
    warnings: list[LogAnalysisConfigWarning],
    uppercase: bool = False,
) -> str:
    """LLM: 把用户配置值安全转换成允许集合中的字符串选项。

    新手说明:
    例如 response_mode 只能是 recommend/dry_run/execute，capability_level 只能是 L0-L5。
    如果用户写了别的值，就回退默认值并记录 warning。

    参数说明:
    field_name: 字段名，用于 warning。
    raw_value: 原始值。
    default: 缺失或坏值时使用的默认选项。
    choices: 允许的字符串集合。
    warnings: warning 列表。
    uppercase: True 表示先转大写再比较，适合 L0-L5。

    返回说明:
    返回 choices 中的字符串，或 default。
    """
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        normalized = normalized.upper() if uppercase else normalized.lower()
        if normalized in choices:
            return normalized
    _warn(warnings, field_name, raw_value, default, f"expected one of {sorted(choices)}")
    return default


def _coerce_int(
    field_name: str,
    raw_value: Any,
    *,
    default: int,
    min_value: int,
    max_value: int | None,
    warnings: list[LogAnalysisConfigWarning],
) -> int:
    """LLM: 把用户配置值安全转换成有范围限制的整数。

    新手说明:
    这个函数接受整数，也接受像 "100" 这样的数字字符串；但不接受 True/False，
    因为布尔值在 Python 里也是 int，直接接受会很容易误判。

    参数说明:
    field_name: 字段名，用于 warning。
    raw_value: 原始值。
    default: 缺失或坏值时使用的默认整数。
    min_value: 允许的最小值。
    max_value: 允许的最大值；None 表示没有上限。
    warnings: warning 列表。

    返回说明:
    返回范围内整数；解析失败或越界时返回 default。
    """
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, bool):
        _warn(warnings, field_name, raw_value, default, "expected an integer, not a boolean")
        return default
    if isinstance(raw_value, int):
        number = raw_value
    elif isinstance(raw_value, str) and _INT_PATTERN.fullmatch(raw_value.strip()):
        number = int(raw_value.strip())
    else:
        _warn(warnings, field_name, raw_value, default, "expected an integer")
        return default

    if number < min_value:
        _warn(warnings, field_name, raw_value, default, f"expected value >= {min_value}")
        return default
    if max_value is not None and number > max_value:
        _warn(warnings, field_name, raw_value, default, f"expected value <= {max_value}")
        return default
    return number


def _coerce_path_string(
    field_name: str,
    raw_value: Any,
    *,
    default: str,
    warnings: list[LogAnalysisConfigWarning],
) -> str:
    """LLM: 校验路径字符串非空且不包含明显危险控制字符。

    新手说明:
    路径配置必须是字符串，不能是空值，也不能包含换行或 NUL 字符。
    这不是完整安全沙盒，只是配置层的基础防呆。

    参数说明:
    field_name: 字段名，用于 warning。
    raw_value: 原始值。
    default: 缺失或坏值时使用的默认路径字符串。
    warnings: warning 列表。

    返回说明:
    返回可接受的路径字符串，或 default。
    """
    if raw_value is _MISSING:
        return default
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        if normalized and "\x00" not in normalized and "\n" not in normalized and "\r" not in normalized:
            return normalized
    _warn(warnings, field_name, raw_value, default, "expected a non-empty path string")
    return default
