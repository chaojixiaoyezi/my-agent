# LLM: Shared delivery recovery bool rendering helper.
# 模块用途: 将恢复合同布尔值渲染为 JSON 风格字符串。

from __future__ import annotations


# LLM: _json_bool renders machine booleans consistently inside model-visible contract hints.
# 函数用途: 把恢复合同里的布尔字段输出为 JSON 风格 true/false，避免大小写不稳定。
def _json_bool(value: object, *, default: bool) -> str:
    return "true" if bool(default if value is None else value) else "false"
