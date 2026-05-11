# LLM: Shared parsing value normalizers keep result parsing small while preserving legacy imports.
# 模块用途: 放置子代理结构化输出里反复使用的 list/dict/int 归一化工具，避免主解析器文件继续膨胀。

from __future__ import annotations


# LLM: _dict_list accepts loose runner JSON but only returns shallow dict objects.
# 函数用途: 把任意输入规范成 dict 列表，供验收、补丁、证据解析复用。
def _dict_list(value: object) -> list[dict[str, object]]:
    """把任意值规范成 dict list。"""

    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            result.append({str(key): val for key, val in item.items()})
    return result


# LLM: _string_list keeps model-provided fields compact and printable.
# 函数用途: 把字符串或字符串列表形态的输入统一转成非空字符串列表。
def _string_list(value: object) -> list[str]:
    """把任意值规范成字符串列表。"""

    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


# LLM: _int_value converts loose numeric config/result fields without raising.
# 函数用途: 把模型输出里的整数、浮点或数字字符串转成 int，无法识别时返回 0。
def _int_value(value: object) -> int:
    """把任意值尽量转成整数，失败时返回 0。"""

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value.strip()))
        except ValueError:
            return 0
    return 0


# LLM: _string_dict normalizes metadata maps for structured reports.
# 函数用途: 把任意 dict 的键和值都转成字符串，非 dict 输入返回空字典。
def _string_dict(value: object) -> dict[str, str]:
    """把任意值规范成字符串字典。"""

    if not isinstance(value, dict):
        return {}
    return {str(key): str(val) for key, val in value.items()}


# LLM: _split_allowed_items separates allowed declarations from ignored ones for policy reports.
# 函数用途: 按允许集合拆分模型声明项，返回已接受项和被忽略项两个列表。
def _split_allowed_items(items: list[str], allowed: set[str]) -> tuple[list[str], list[str]]:
    """拆分授权项和未授权项。"""

    accepted: list[str] = []
    ignored: list[str] = []
    for item in items:
        if item in allowed:
            accepted.append(item)
        else:
            ignored.append(item)
    return accepted, ignored


# LLM: _normalize_runner_items makes runner artifacts/tests/patches safe to persist as JSON.
# 函数用途: 把 runner 条目列表转成浅层、可 JSON 化的对象列表。
def _normalize_runner_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """把 runner item 转成稳定可 JSON 化的浅层对象。"""

    normalized: list[dict[str, object]] = []
    for item in items:
        normalized.append(_normalize_single_runner_item(item))
    return normalized


# LLM: _normalize_single_runner_item stringifies one runner item without mutating the source.
# 函数用途: 归一化单个 runner 条目，确保键是字符串且值能安全写入 JSON。
def _normalize_single_runner_item(item: dict[str, object]) -> dict[str, object]:
    """Normalize one runner item dict to JSON-serializable form."""
    payload: dict[str, object] = {}
    for key, value in item.items():
        payload[str(key)] = _normalize_runner_value(value)
    return payload


# LLM: _normalize_runner_value avoids leaking unserializable Python objects into result files.
# 函数用途: 把单个 runner 字段值转成字符串、基础类型、字符串列表或字符串字典。
def _normalize_runner_value(value: object) -> object:
    """Normalize a single runner field value to JSON-serializable form."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [str(entry) for entry in value]
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return str(value)
