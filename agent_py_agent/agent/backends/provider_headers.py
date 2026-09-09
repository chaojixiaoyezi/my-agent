# LLM: 请求头仅由显式配置和宿主会话身份生成；禁止覆盖认证/传输边界，主子及辅助请求复用此实现。
# 模块用途: 校验自定义请求头并附加稳定的匿名会话编号，不复制其它客户端身份。
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

_SESSION: ContextVar[str] = ContextVar("provider_session", default="")
_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_PROTECTED = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "proxy-authorization",
    "proxy-authenticate", "te", "trailer", "upgrade", "accept-encoding", "content-type",
    "authorization", "x-api-key", "x-goog-api-key", "cookie", "set-cookie",
    "chatgpt-account-id", "session_id", "x-client-request-id", "x-codex-window-id",
    "forwarded", "x-forwarded-for", "x-real-ip", "cf-connecting-ip",
})


# LLM: 参考 CCSwitch header override 防护；错误只含固定文案，不输出可能含密钥的字段值。
# 函数用途: 拒绝头注入、认证替换和大小写重复，返回独立配置副本。
def validate_headers(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("自定义请求头须为最多 32 项的 JSON 对象。")
    result = {}
    seen = set()
    for name, text in value.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name) or len(name) > 128:
            raise ValueError("请求头名称不合法。")
        lower = name.lower()
        if lower in _PROTECTED or lower in seen:
            raise ValueError("请求头重复或属于认证/传输保护字段，请使用专门配置项。")
        if not isinstance(text, str) or len(text) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in text):
            raise ValueError("请求头值须为不含换行的 ASCII 文本。")
        result[name] = text
        seen.add(lower)
    return result


# LLM: 动态会话头名称是配置，值永远来自宿主身份，不能接受静态 UUID 冒充会话。
# 函数用途: 校验可选会话头；空字符串表示不附加。
def validate_session_header(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("会话头名称须为字符串。")
    if value:
        validate_headers({value: "session"})
        if value.lower() in {"user-agent", "accept", "anthropic-version"}:
            raise ValueError("会话头不能占用客户端或协议字段。")
    return value


# LLM: owner/thread 是宿主精确事实，哈希用于不向外泄露内部身份；同 thread 多轮/Compact 复用，退出必恢复。
# 函数用途: 让并发模型请求各自带上自己的稳定会话编号，不改共享 backend。
@contextmanager
def provider_session_scope(owner: tuple[str, ...], thread_id: str):
    identity = hashlib.sha256(json.dumps([*owner, thread_id], ensure_ascii=True).encode()).hexdigest() if thread_id else ""
    token = _SESSION.set(identity)
    try:
        yield
    finally:
        _SESSION.reset(token)


# LLM: 优先使用宿主 agent_thread_id，子代理不借父 thread；无持久身份的一次独立调用只生成一次临时会话。
# 函数用途: 在整个工作片外层绑定会话，让探针、正文、Compact 和保护线程一致继承。
@contextmanager
def provider_runtime_scope(agent: object, params: object):
    attrs = getattr(params, "task_attributes", None) or {}
    thread_id = str(attrs.get("agent_thread_id") or attrs.get("conversation_thread_id") or "")
    thread_id = thread_id or str(getattr(params, "thread_id", "") or "")
    if not thread_id and _SESSION.get():
        yield
        return
    thread_id = thread_id or str(getattr(params, "run_id", "") or getattr(params, "task_id", "") or uuid4())
    home = getattr(agent, "home_paths", None)
    owner = tuple(str(getattr(home, key, "")) for key in ("owner_provider", "owner_kind", "owner_id"))
    with provider_session_scope(owner, thread_id):
        yield


# LLM: 缺宿主会话时拒绝发送所需 session header，不生成每轮随机 ID 破坏路由和缓存；不热改配置。
# 函数用途: 合并已验证请求头，保留宿主认证，并按需追加会话号。
def request_headers(headers: dict[str, str], custom: dict[str, str], session_header: str) -> dict[str, str]:
    merged = {"User-Agent": "my-agent/1.0", **headers}
    for name, value in validate_headers(custom).items():
        merged = {key: text for key, text in merged.items() if key.lower() != name.lower()}
        merged[name] = value
    if session_header:
        validate_session_header(session_header)
        if not _SESSION.get():
            raise ValueError("此服务商需要会话编号，但当前请求没有绑定宿主会话。")
        merged = {key: text for key, text in merged.items() if key.lower() != session_header.lower()}
        merged[session_header] = _SESSION.get()
    return merged


# LLM: 只规范化显式 endpoint 后缀，不猜模型或跨域改路由；基础路径前缀必须原样保留。
# 函数用途: 避免粘贴完整接口或 /v1 基础地址时重复拼出 /v1/v1/messages。
def endpoint_parts(base: str, path: str) -> tuple[str, str]:
    base = base.rstrip("/")
    if base.endswith(path):
        return base, ""
    if base.endswith("/v1") and path.startswith("/v1/"):
        return base, path[3:]
    return base, path
