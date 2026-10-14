# LLM: 凭据表单与认证方案由宿主受控 schema 验证；字段只描述输入，不接受模型提供秘密或任意脚本。
# 模块用途: 校验私人多字段卡、有效期及 HTTP 登录/签名方案，复用原 TaskCredential 存储和网络授权。
from __future__ import annotations

import re

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_HEADER = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}\Z")
_FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "proxy-authorization"}


# LLM: 字段标识是准确映射，不用 label/自然语言推断用途；一到八字段均按秘密处理。
# 函数用途: 标准认证生成默认表单，其它方式必须提供明确字段；所有值只由私人卡回调取得。
def credential_fields(authentication: str, fields: object = None) -> list[dict[str, str]]:
    if fields is None:
        fields = ([{"name": "username", "label": "账号"}, {"name": "secret", "label": "密码"}]
                  if authentication == "basic" else [{"name": "secret", "label": "API Key / Token"}])
    if not isinstance(fields, list) or not 1 <= len(fields) <= 8:
        raise ValueError("凭据表单需要一到八个字段")
    result = []
    for field in fields:
        if not isinstance(field, dict) or set(field) != {"name", "label"}:
            raise ValueError("凭据字段只能声明 name 和 label")
        name, label = field["name"], field["label"]
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(label, str) or not 1 <= len(label) <= 64:
            raise ValueError("凭据字段名称无效")
        result.append({"name": name, "label": label})
    names = {field["name"] for field in result}
    required = {"username", "secret"} if authentication == "basic" else {"secret"}
    if len(names) != len(result) or (authentication in {"basic", "bearer", "header"} and names != required):
        raise ValueError("凭据字段重复或与认证方式不符")
    return result


# LLM: 认证头不得重写 HTTP 目标或 framing；真实目标仍由同 origin 和原网络出口双重核验。
# 函数用途: 检查宿主认证可注入的头名。
def credential_header_name(value: object) -> str:
    if not isinstance(value, str) or not _HEADER.fullmatch(value) or value.lower() in _FORBIDDEN_HEADERS:
        raise ValueError("认证请求头无效")
    return value


# LLM: 数值不能来自布尔/NaN/无限；上下限同时约束输入卡和 ready 凭据寿命。
# 函数用途: 规范显式有效秒数，避免永不过期的隐式授权。
def credential_seconds(value: object, *, default: int, maximum: int = 31536000) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 60 <= value <= maximum:
        raise ValueError("有效期必须是 60 秒到一年之间的整数")
    return value


# LLM: 登录和签名只允许声明式映射；没有 eval、shell、任意代码或不受控目标，参数不含秘密值。
# 函数用途: 验证认证方案并返回可持久化的规范副本。
def credential_auth_config(authentication: str, value: object, fields: list[dict[str, str]]) -> dict:
    config = {} if value is None else value
    if not isinstance(config, dict):
        raise ValueError("认证方案必须是对象")
    names = {field["name"] for field in fields}
    if authentication in {"basic", "bearer", "header"}:
        if config:
            raise ValueError("此认证方式不需要额外方案")
        return {}
    if authentication == "login":
        return _login_config(config, names)
    if authentication == "signature":
        return _signature_config(config, names)
    raise ValueError("认证方式无效")


# LLM: login URL 的 origin 由记录入口再次验证；令牌路径只读取 JSON 字段，不运行表达式。
# 函数用途: 校验 JSON 登录、可选 Cookie/CSRF 和有限期会话缓存。
def _login_config(config: dict, names: set[str]) -> dict:
    allowed = {"url", "body_fields", "token_path", "token_header", "token_prefix", "cookies", "csrf_cookie", "csrf_header", "session_seconds"}
    if set(config) - allowed or not isinstance(config.get("url"), str):
        raise ValueError("登录方案字段无效")
    mapping = config.get("body_fields")
    if not isinstance(mapping, dict) or not mapping or any(not isinstance(k, str) or not _NAME.fullmatch(k) or v not in names for k, v in mapping.items()):
        raise ValueError("登录字段必须引用表单字段")
    path = config.get("token_path", [])
    if not isinstance(path, list) or len(path) > 8 or any(not isinstance(v, str) or not _NAME.fullmatch(v) for v in path):
        raise ValueError("令牌路径无效")
    cookies = config.get("cookies", False)
    if not isinstance(cookies, bool) or not (path or cookies):
        raise ValueError("登录必须声明令牌或 Cookie")
    prefix = config.get("token_prefix", "Bearer ")
    if not isinstance(prefix, str) or len(prefix) > 32 or "\r" in prefix or "\n" in prefix:
        raise ValueError("令牌前缀无效")
    csrf = config.get("csrf_cookie", "")
    if csrf and (not cookies or not isinstance(csrf, str) or not _NAME.fullmatch(csrf)):
        raise ValueError("CSRF Cookie 无效")
    return {"url": config["url"], "body_fields": dict(mapping), "token_path": list(path),
            "token_header": credential_header_name(config.get("token_header", "Authorization")),
            "token_prefix": prefix, "cookies": cookies, "csrf_cookie": csrf,
            "csrf_header": credential_header_name(config.get("csrf_header", "X-CSRF-Token")),
            "session_seconds": credential_seconds(config.get("session_seconds"), default=300, maximum=86400)}


# LLM: 签名只读取准确声明的原始字段和本次 HTTP 请求事实，输出固定头；不会提供签名 oracle 给任意目标。
# 函数用途: 校验 SHA256/HMAC-SHA256 签名组成与秘密字段引用。
def _signature_config(config: dict, names: set[str]) -> dict:
    if set(config) - {"algorithm", "secret_field", "header", "parts"} or config.get("algorithm") not in {"hmac-sha256", "sha256"}:
        raise ValueError("签名算法无效")
    secret = config.get("secret_field", "secret")
    parts = config.get("parts")
    if secret not in names or not isinstance(parts, list) or not 1 <= len(parts) <= 16:
        raise ValueError("签名字段无效")
    for part in parts:
        if not isinstance(part, dict) or len(part) != 1:
            raise ValueError("签名组成无效")
        kind, value = next(iter(part.items()))
        valid = ((kind == "field" and value in names) or (kind == "literal" and isinstance(value, str) and len(value) <= 128)
                 or (kind == "request" and value in {"method", "path", "body_sha256", "timestamp"}))
        if not valid:
            raise ValueError("签名组成无效")
    return {"algorithm": config["algorithm"], "secret_field": secret,
            "header": credential_header_name(config.get("header", "X-Signature")), "parts": list(parts)}
