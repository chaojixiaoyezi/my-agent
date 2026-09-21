# LLM: 这里只校验 JSON 传输事实，不裁决领域 schema；重复键、非有限数及非法 UTF-8 不能静默修正。
# 模块用途: 为插件包、安装事实与原工具操作回读提供同一个严格 JSON 读取入口。

from __future__ import annotations

import json


# LLM: 调用方先限制字节大小，并自行校验根结构与版本；失败不记录正文或路径。
# 函数用途: 读取唯一键、有限数值的 UTF-8 JSON，拒绝解析器的宽松扩展。
def load_strict_json(content: bytes | str) -> object:
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    value = json.loads(text, object_pairs_hook=_unique_fields, parse_constant=_invalid_constant)
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return value


# LLM: 同一字段不能用后出现的值覆盖已验证身份或提交标识；每层对象都执行检查。
# 函数用途: 从 JSON 字段序列生成唯一键对象。
def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 字段重复")
        result[key] = value
    return result


# LLM: NaN/Infinity 不是协议 JSON；错误只供调用方分类，不从正文推断业务状态。
# 函数用途: 拒绝 Python JSON 默认支持的非有限数扩展。
def _invalid_constant(value: str) -> object:
    raise ValueError("JSON 数值无效")
