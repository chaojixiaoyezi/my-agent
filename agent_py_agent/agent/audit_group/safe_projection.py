# LLM: 所有公开DTO共用此有界投影；schema扩展、异常正文和路径不构成公开许可。
# 模块用途: 递归裁剪Schema并隐藏秘密和宿主绝对路径。
from __future__ import annotations

import json
import re

from ..common.log_redaction import redact_sensitive_value

FORBIDDEN = frozenset({"code", "source_code", "body_markdown", "steps", "prompt", "credential_refs",
                       "data_sources", "datasource", "workflow_snapshot", "owner_home", "path", "entrypoint_path"})
_SCHEMA_CHILDREN = frozenset({"items", "prefixItems", "additionalItems", "contains", "additionalProperties",
                            "propertyNames", "not", "if", "then", "else", "allOf", "anyOf", "oneOf"})
_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "defs", "dependencies", "dependentSchemas"})
_SCHEMA_KEYS = frozenset({"type", "title", "description", "default", "examples", "enum", "const", "required",
    "properties", "items", "additionalProperties", "minimum", "maximum", "minLength", "maxLength",
    "minItems", "maxItems", "format", "oneOf", "anyOf", "allOf", "nullable"}) | _SCHEMA_CHILDREN | _SCHEMA_MAPS
_PATH = re.compile(r"(?<![\w:])(?:[A-Za-z]:[\\/]|~[/\\]|/)[^\s\"'<>;,\]\)}]*")
_SENSITIVE_PARTS = ("api_key", "apikey", "passphrase", "password", "passwd", "secret", "token", "credential",
    "private_key", "access_key", "client_secret", "encrypt_key", "encryption_key", "signing_key", "webhook_secret",
    "session_key", "auth_key", "bearer", "pwd", "cookie", "session", "authorization", "webhook")
_SENSITIVE_TOKENS = frozenset({"pw", "key", "pin", "jwt", "pat", "ak", "sk"})
_SCHEMA_LITERALS = frozenset({"default", "examples", "enum", "const"})


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


# LLM: 长秘密名称按子串命中，短词仅按分隔后的整词命中；保留既有cookie/session等秘密分类。
# 函数用途: 统一识别点号、连字符和下划线形式的敏感属性名。
def _is_sensitive_key(key):
    normalized = key.lower().replace("-", "_").replace(".", "_")
    return any(part in normalized for part in _SENSITIVE_PARTS) or bool(_SENSITIVE_TOKENS.intersection(normalized.split("_")))


# LLM: 命名schema继承父敏感性并核子名称，不把映射值交给普通数据投影；仍限制名称长度和私有字段。
# 函数用途: 安全投影properties、defs及依赖schema映射。
def _schema_mapping(value, *, depth, budget, sensitive):
    if type(value) is not dict:
        return {}
    return {name: parameters_schema(schema, depth=depth + 1, budget=budget,
                                    sensitive=sensitive or _is_sensitive_key(name))
            for name, schema in list(value.items())[:64] if type(name) is str and 0 < len(name) <= 64
            and name.lower() not in FORBIDDEN and safe_text(name) == name}


# LLM: 敏感属性上下文穿透列表、组合/条件及命名schema；沿原节点/深度预算裁剪，字面值省略。
# 函数用途: 为跨owner概况保留有界入参说明，敏感字段不暴露默认值、示例或候选值。
def parameters_schema(value, *, depth=0, budget=None, sensitive=False):
    budget = [256] if budget is None else budget
    budget[0] -= 1
    if depth > 6 or budget[0] < 0:
        return {}
    if type(value) is list:
        return [parameters_schema(item, depth=depth + 1, budget=budget, sensitive=sensitive) for item in value[:64]]
    if type(value) is bool:
        return value
    if type(value) is not dict:
        return {}
    result = {}
    for key, item in list(value.items())[:64]:
        if key not in _SCHEMA_KEYS or sensitive and key in _SCHEMA_LITERALS:
            continue
        if key in _SCHEMA_MAPS:
            result[key] = _schema_mapping(item, depth=depth, budget=budget, sensitive=sensitive)
        elif key in _SCHEMA_CHILDREN:
            child = item[:8] if key in {"oneOf", "anyOf", "allOf"} and type(item) is list else item
            result[key] = parameters_schema(child, depth=depth + 1, budget=budget, sensitive=sensitive)
        else:
            result[key] = public_value(item)
    return result


# LLM: 私密原文只驻留宿主内存，返回集合不可序列化或当公开内容；沿用源定义隐藏字段边界。
# 函数用途: 收集已知的源代码、正文和私有配置片段供结果与文件共同抹除。
def private_strings(records):
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
    return sorted(private, key=len, reverse=True)


# LLM: 输出文件只支持有界UTF8文本；先精确抹源私密片段再通用脱敏，不声称阻断任意编码信息流。
# 函数用途: 净化产物正文或文件名，不使用公开DTO的4096字符截断。
def scrub_private_text(text, records):
    for value in private_strings(records):
        text = text.replace(value, "<private>")
    return safe_text(text, len(text) + 1024)


# LLM: 已知源代码/正文/私有配置仅用于宿主内存内的精确抹除；不能声称能识别任意恶意编码的信息流。
# 函数用途: 公开结果前移除可精确识别的私密原文，连同动态字典键一并净化。
def scrub_private_result(result, records):
    text = json.dumps(result, ensure_ascii=False, allow_nan=False)
    for value in private_strings(records):
        escaped = json.dumps(value, ensure_ascii=False)[1:-1]
        text = text.replace(escaped, "<private>")
    return public_value(json.loads(text))
