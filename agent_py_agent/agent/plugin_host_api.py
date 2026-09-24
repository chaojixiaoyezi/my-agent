# LLM: 插件宿主只读 API 的唯一实现：令牌只发给包描述声明了 host_api=["read"] 的插件激活，按激活绑定、每次请求复核激活仍是
#   同一代（停用、卸载、换代即失效）；只返回公开白名单投影（线程列表、活动/上下文数字、插件列表），不含消息正文、路径、配置或密钥，
#   不提供任何写入或控制。地址只在 Gateway HTTP 服务运行时存在（只绑回环）。修改须同步 PLUGIN_HOST_API.md 与 test_plugin_host_api。
# 模块用途: 让网页控制台、桌面窗口这类界面型插件能读取宿主运行状态，而不需要拿到 owner 身份或直接调用 Gateway 内部接口。

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass

HOST_API_READ = "read"
HOST_API_URL_ENV = "MY_AGENT_HOST_API_URL"
HOST_API_TOKEN_ENV = "MY_AGENT_HOST_API_TOKEN"
HOST_API_PATH = "/plugin-host/query"
HOST_API_TOPICS = ("threads", "activity", "plugins", "gateway")
MAX_THREADS = 20
_TITLE_CHARS = 60

_LOCK = threading.Lock()
_BASE_URL: str | None = None
_GRANTS: dict[str, _Grant] = {}


# LLM: 令牌到固定激活引用的绑定；不保存 owner 实例或设置。
# 类用途: 记录一枚宿主 API 令牌属于哪个插件激活。
@dataclass(frozen=True)
class _Grant:
    activation_ref: object
    plugin_id: str


# LLM: 只由 Gateway HTTP 服务在启动/停止时调用；None 表示当前进程没有可用的宿主 API（例如直连模式）。
# 函数用途: 设置或清除宿主 API 的回环地址。
def set_host_api_base(url: str | None) -> None:
    global _BASE_URL
    with _LOCK:
        _BASE_URL = url
        if url is None:
            _GRANTS.clear()


# LLM: 仅在构造插件客户端时调用；宿主 API 不可用时返回 None（插件应显示"宿主 API 不可用"）。副作用：登记令牌。
# 函数用途: 为一个插件激活发放宿主 API 地址和令牌，返回要放进插件进程环境的变量。
def issue_host_api_env(activation_ref, plugin_id: str) -> dict[str, str]:
    with _LOCK:
        if _BASE_URL is None:
            return {}
        token = secrets.token_urlsafe(32)
        _GRANTS[token] = _Grant(activation_ref, plugin_id)
        return {HOST_API_URL_ENV: _BASE_URL + HOST_API_PATH, HOST_API_TOKEN_ENV: token}


# LLM: 激活失效（停用、卸载、换代或安装表不可读）时删除令牌并返回 None；不追随新代次。
# 函数用途: 校验令牌并返回仍然有效的授权。
def verify_host_api_token(token: str) -> _Grant | None:
    with _LOCK:
        grant = _GRANTS.get(token)
    if grant is None:
        return None
    try:
        current = grant.activation_ref.require()
        valid = getattr(current.activation, "activation_id", None) == grant.activation_ref.scope.activation_id
    except (OSError, ValueError):
        valid = False
    if not valid:
        with _LOCK:
            _GRANTS.pop(token, None)
        return None
    return grant


# LLM: 线程只给编号、截短标题、状态、时间与压缩代次；读取失败的线程直接跳过，不暴露错误路径。
# 函数用途: 生成 owner 最近线程的公开列表。
def _threads(owner_agent) -> list[dict]:
    store = getattr(owner_agent, "conversation_store", None)
    if store is None:
        return []
    threads, _errors = store.threads.list_report(limit=MAX_THREADS)
    rows = []
    for thread in reversed(threads):
        title = " ".join(str(getattr(thread, "title", "") or "").split())
        rows.append({"thread_id": thread.thread_id, "title": title[:_TITLE_CHARS], "status": str(thread.status or ""),
                     "updated_at": float(getattr(thread, "updated_at", 0.0) or 0.0),
                     "compact_generation": int(getattr(thread, "compact_generation", 0) or 0)})
    return rows


# LLM: 复用展示服务的主题投影（活动、运行状态、上下文数字），与面板看到的是同一份公开数据。
# 函数用途: 返回某个线程的活动与上下文投影。
def _activity(owner_agent, thread_id: str) -> dict:
    from .conversation.agent_activity import conversation_agent_activity
    from .plugin_display.service import project_topics

    store = getattr(owner_agent, "conversation_store", None)
    if store is None or not thread_id or store.threads.load(thread_id) is None:
        return {}
    activity = conversation_agent_activity(owner_agent, store, thread_id).to_dict()
    return project_topics(activity, ("activity", "run_state", "context"))


# LLM: 插件列表只含 ID、版本、启用状态与简介，不含设置、环境路径或激活编号。
# 函数用途: 返回本 owner 已安装插件的公开列表。
def _plugins(owner) -> list[dict]:
    from .plugin_install_store import PluginInstallStore

    return [{"plugin_id": row.manifest.plugin_id, "version": row.manifest.version, "enabled": bool(row.enabled),
             "summary": row.manifest.summary} for row in PluginInstallStore(owner).snapshot()]


# LLM: 主题白名单外的请求直接拒绝；owner 未加载时线程/活动为空并标注，不为插件加载 owner 实例。
# 函数用途: 按请求的主题组装只读结果。
def query_host(base_agent, grant: _Grant, body: dict, *, gateway_status: dict | None = None) -> dict:
    from .gateway_parts.request_worker import _resolve_loaded_request_agent_for_owner

    topics = body.get("topics")
    if not isinstance(topics, list) or not topics or any(topic not in HOST_API_TOPICS for topic in topics):
        raise ValueError("topics 必须是非空数组，且只能取 " + "、".join(HOST_API_TOPICS))
    owner = grant.activation_ref.owner()
    owner_agent = _resolve_loaded_request_agent_for_owner(base_agent, owner.identity)
    result: dict[str, object] = {"owner_loaded": owner_agent is not None, "at": time.time()}
    if "threads" in topics:
        result["threads"] = _threads(owner_agent) if owner_agent is not None else []
    if "activity" in topics:
        thread_id = body.get("thread_id")
        if not isinstance(thread_id, str):
            raise ValueError("activity 主题需要 thread_id")
        result["activity"] = _activity(owner_agent, thread_id) if owner_agent is not None else {}
    if "plugins" in topics:
        result["plugins"] = _plugins(owner)
    if "gateway" in topics:
        result["gateway"] = dict(gateway_status or {})
    return result


# LLM: 回环来源检查先于读正文；令牌放在 X-Plugin-Host-Token 头，不接受查询参数；错误不回显令牌。
# 函数用途: 处理 POST /plugin-host/query。
def handle_plugin_host_query(handler, server) -> None:
    from .gateway_parts.http_handlers import require_trusted_source

    if require_trusted_source(handler):
        return
    grant = verify_host_api_token(str(handler.headers.get("X-Plugin-Host-Token") or ""))
    if grant is None:
        handler._send_json(403, {"ok": False, "error": "宿主 API 令牌无效或插件已停用"})
        return
    try:
        body = handler._read_json()
        status = {"pid": os.getpid(), "port": getattr(server, "port", None)}
        handler._send_json(200, {"ok": True, **query_host(server.agent, grant, body if isinstance(body, dict) else {},
                                                          gateway_status=status)})
    except (ValueError, TypeError) as exc:
        handler._send_json(400, {"ok": False, "error": str(exc)})
