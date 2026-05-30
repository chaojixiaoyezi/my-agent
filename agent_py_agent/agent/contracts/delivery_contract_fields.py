# LLM: Delivery contract field helpers normalize open-world field specs without hard enums.
# 模块用途: 把合同里的字符串字段或带名称的字段对象归一成字符串列表，供 doctor/验收复用。

from __future__ import annotations

from typing import Any

_NAMED_FIELD_KEYS = ("column_name", "name", "field", "key", "label", "title")


# LLM: string_items converts mixed field declarations into clean string names.
# 函数用途: 支持字符串列表和带名称字段对象两种写法，过滤坏条目而不是丢整段。
def string_items(value: object, *, allow_named_dict: bool = False) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [_string_item(item, allow_named_dict=allow_named_dict) for item in value]
    return [item for item in items if item]


# LLM: _string_item extracts one string field declaration.
# 函数用途: 从字符串或可选命名对象里取出字段名，无法识别时返回空串让上层跳过。
def _string_item(value: object, *, allow_named_dict: bool) -> str:
    if isinstance(value, str):
        return value.strip()
    if not allow_named_dict or not isinstance(value, dict):
        return ""
    return _named_string_item(value)


# LLM: _named_string_item finds the first supported name key in field objects.
# 函数用途: 兼容 column_name/name/field/key 等常见字段名声明，保持合同格式开放。
def _named_string_item(value: dict[str, Any]) -> str:
    for key in _NAMED_FIELD_KEYS:
        item = str(value.get(key) or "").strip()
        if item:
            return item
    return ""
