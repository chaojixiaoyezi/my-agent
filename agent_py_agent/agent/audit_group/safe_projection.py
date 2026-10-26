# LLM: 所有公开DTO共用此有界投影；schema扩展、异常正文和路径不构成公开许可。
# 模块用途: 递归裁剪Schema并隐藏秘密和宿主绝对路径。
from __future__ import annotations

import json
import re

from ..common.log_redaction import redact_sensitive_value

FORBIDDEN = frozenset({"code", "source_code", "body_markdown", "steps", "prompt", "credential_refs",
                       "data_sources", "datasource", "workflow_snapshot", "owner_home", "path", "entrypoint_path"})
_SCHEMA_KEYS = frozenset({"type", "title", "description", "default", "examples", "enum", "const", "required",
    "properties", "items", "additionalProperties", "minimum", "maximum", "minLength", "maxLength",
    "minItems", "maxItems", "format", "oneOf", "anyOf", "allOf", "nullable"})
_PATH = re.compile(r"(?<![\w:])(?:[A-Za-z]:[\\/]|~[/\\]|/)[^\s\"'<>;,\]\)}]*")


# LLM: 先打码再限制长度，避免截断凭据语法后无法识别；不读取路径。
# 函数用途: 将公开短文本中的秘密和绝对路径替换为标记。
def safe_text(value, limit=1024):
    if type(value) is not str:
        return ""
    return _PATH.sub("<path>", redact_sensitive_value(value))[:limit]


# LLM: 节点数、深度、字符串及序列长度均有界；禁止内部字段在任意嵌套层回流。
# 函数用途: 将结构化输出裁成安全JSON副本，不保留源对象引用。
def public_value(value, *, depth=0, budget=None):
    budget = [512] if budget is None else budget
    budget[0] -= 1
    if depth > 8 or budget[0] < 0:
        return None
    if isinstance(value, str):
        return safe_text(value, 4096)
    if type(value) in (bool, int, float) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [public_value(item, depth=depth + 1, budget=budget) for item in value[:64]]
    if type(value) is dict:
        clean = {safe_text(key, 128): public_value(item, depth=depth + 1, budget=budget)
                 for key, item in list(value.items())[:64] if type(key) is str and key.lower() not in FORBIDDEN}
        return redact_sensitive_value(clean)
    return None


# LLM: 仅保留声明的JSONSchema关键字；$ref和扩展不公开，属性名本身亦需无秘密无路径。
# 函数用途: 为跨owner概况保留有界、安全的入参说明。
def parameters_schema(value, *, depth=0, budget=None):
    budget = [256] if budget is None else budget
    budget[0] -= 1
    if type(value) is not dict or depth > 6 or budget[0] < 0:
        return {}
    result = {}
    for key, item in list(value.items())[:64]:
        if key not in _SCHEMA_KEYS:
            continue
        if key == "properties" and type(item) is dict:
            result[key] = {name: parameters_schema(schema, depth=depth + 1, budget=budget)
                           for name, schema in list(item.items())[:64] if type(name) is str
                           and name.lower() not in FORBIDDEN and re.fullmatch(r"[\w.-]{1,64}", name)
                           and safe_text(name) == name}
        elif key in {"items", "additionalProperties"} and type(item) is dict:
            result[key] = parameters_schema(item, depth=depth + 1, budget=budget)
        elif key in {"oneOf", "anyOf", "allOf"} and type(item) is list:
            result[key] = [parameters_schema(schema, depth=depth + 1, budget=budget) for schema in item[:8]]
        else:
            result[key] = public_value(item)
    return result


# LLM: 已知源代码/正文/私有配置仅用于宿主内存内的精确抹除；不能声称能识别任意恶意编码的信息流。
# 函数用途: 公开结果前移除可精确识别的私密原文，连同动态字典键一并净化。
def scrub_private_result(result, records):
    private = set()
    pending = [(raw, False) for raw in records.values()]
    while pending:
        value, hidden = pending.pop()
        if type(value) is dict:
            pending.extend((item, hidden or key in FORBIDDEN) for key, item in value.items())
        elif isinstance(value, (list, tuple)):
            pending.extend((item, hidden) for item in value)
        elif hidden and type(value) is str and len(value) >= 4:
            private.update(line.strip() for line in value.splitlines() if len(line.strip()) >= 4)
            private.add(value)
    text = json.dumps(result, ensure_ascii=False, allow_nan=False)
    for value in sorted(private, key=len, reverse=True):
        escaped = json.dumps(value, ensure_ascii=False)[1:-1]
        text = text.replace(escaped, "<private>")
    return public_value(json.loads(text))
