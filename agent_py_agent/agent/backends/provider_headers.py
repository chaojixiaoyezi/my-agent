# LLM: 请求头仅由显式配置和宿主会话身份生成；禁止覆盖认证/传输边界，主子及辅助请求复用此实现。
# 模块用途: 校验自定义请求头并附加稳定的匿名会话编号，不复制其它客户端身份。
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID, uuid4

_SESSION: ContextVar[str] = ContextVar("provider_session", default="")
# ChatGPT 订阅接口按这个请求头决定提示缓存亲和（官方 Codex codex-rs/core/src/client.rs 的 responses_session_id；
#   codex-api build_session_headers 的头名）。不带它时同一会话的请求会落到不同缓存分片，命中率低。
CHATGPT_SESSION_HEADER_PROTOCOL = "session-id"
# LLM: 探测记账只在该 scope 内生效：宿主在探测前绑定账本与请求身份，http 探测每次真实请求按它记账；
#   没有 scope 的调用（测试直调 backend、旧路径）保持不记账，不改变探测行为与缓存。
_PROBE_ACCOUNTING: ContextVar[object | None] = ContextVar("probe_accounting", default=None)


# LLM: ledger 是唯一模型调用账本；request_id/run_id 决定探测用量归到哪个请求的累计 scope，
#   与主请求收尾的快照同一身份，快照才能把探测一并算进去。纯身份容器，不含密钥与正文。
# 类用途: 一次工具能力探测要记入的账本与请求归属。
@dataclass(frozen=True)
class ProbeAccountingScope:
    ledger: object
    request_id: str = ""
    run_id: str = ""


# LLM: 嵌套探测不可能发生（单线程内 select_tool_protocol 同步调用）；scope 退出必须恢复原值，防止串到相邻请求。
# 函数用途: 在探测调用期间绑定记账范围，退出即恢复。
@contextmanager
def probe_accounting_scope(scope: ProbeAccountingScope):
    token = _PROBE_ACCOUNTING.set(scope)
    try:
        yield
    finally:
        _PROBE_ACCOUNTING.reset(token)


# 函数用途: 读取当前线程的探测记账范围；没有绑定时返回 None（不记账）。
def current_probe_accounting() -> ProbeAccountingScope | None:
    value = _PROBE_ACCOUNTING.get()
    return value if isinstance(value, ProbeAccountingScope) else None
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


# LLM: 只读暴露当前线程绑定的稳定会话编号；未绑定时返回空串，绝不生成随机键，供请求体缓存键等公开入口复用。
# 函数用途: 返回当前 provider 会话编号（owner+thread 的 sha256），未绑定会话时返回空串；Responses 请求体用它做提示缓存键。
def current_provider_session() -> str:
    return _SESSION.get()


# LLM: 订阅登录（auth mode=chatgpt）专用的会话编号形态：官方 Codex 根代理的 session-id 头与 prompt_cache_key 是同一个 UUID，
#   服务端按 session-id 把同一会话路由到同一份提示缓存。这里把宿主会话编号（owner+thread 的 sha256）前 128 位排成 UUID：
#   同线程稳定、跨线程不同、不含凭据；未绑定会话返回空串（缓存亲和是优化，不是协议必需），绝不生成随机值。
#   改动同步 test_responses_cache_key 与 test_model_oauth 的订阅用例。
# 函数用途: 返回订阅接口用的会话 UUID；没有绑定宿主会话时返回空串。
def chatgpt_session_uuid() -> str:
    session = _SESSION.get()
    return str(UUID(hex=session[:32])) if session else ""


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
