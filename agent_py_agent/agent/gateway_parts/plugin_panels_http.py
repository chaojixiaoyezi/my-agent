# LLM: /client/plugin-panels 入口：可信来源检查先于读正文；owner 与线程只由 Gateway 作用域解析，客户端只能选择面板。
#   活动投影复用 conversation_agent_activity 的同一只读结果，冷 owner 用空投影；不写任何状态，不加载 owner 实例。
# 模块用途: 把 TUI 打开的插件面板请求交给进程内唯一的展示服务，返回已校验的面板内容。
from __future__ import annotations

import threading

_SERVICE_LOCK = threading.Lock()


# LLM: 请求体只接受 conversation_id 与 panels=[{plugin_id, panel_id}]；数量上限由服务裁决，类型错误直接 400。
# 函数用途: 处理 TUI 的插件面板查询。
def handle_client_plugin_panels(handler, server) -> None:
    from ..plugin_display.service import PanelQuery
    from .http_handlers import _gateway_control_scope, _request_channel, require_trusted_source

    if require_trusted_source(handler):
        return
    if server is None or server.agent is None:
        handler._send_json(500, {"error": "server client service not initialized"})
        return
    try:
        body = handler._read_json()
        requested = _requested_panels(body)
    except (ValueError, TypeError) as exc:
        handler._send_json(400, {"error": str(exc)})
        return
    base = server.agent
    if not getattr(base.config, "enable_plugins", False):
        handler._send_json(200, {"ok": True, "panels": []})
        return
    user_id, channel = _request_channel(handler)
    scope = _gateway_control_scope(handler, body, user_id=user_id, channel=channel)
    try:
        owner, thread_key, activity = _owner_and_activity(base, scope)
        sessions = _owner_sessions(base, scope)
    except Exception:  # noqa: BLE001 作用域解析失败不能泄露路径，也不能影响核心接口
        handler._send_json(200, {"ok": False, "panels": [], "error": "owner scope unavailable"})
        return
    service = plugin_display_service(server)
    panels = service.panels(PanelQuery(owner, str(owner.home_dir), thread_key, activity, requested, sessions))
    handler._send_json(200, {"ok": True, "panels": panels})


# LLM: 服务挂在唯一 HTTP server 上，首次请求时创建；Gateway 停止时由 server.stop 关闭。
# 函数用途: 取得（必要时创建）本 Gateway 进程的插件展示服务。
def plugin_display_service(server):
    from ..plugin_display.service import PluginDisplayService

    with _SERVICE_LOCK:
        service = getattr(server, "plugin_display", None)
        if service is None:
            service = PluginDisplayService()
            server.plugin_display = service
        return service


# LLM: 只校验形状，不校验插件是否存在；存在性由服务按安装表裁决并返回 unavailable。
# 函数用途: 从请求体取出要查看的 (插件, 面板) 列表。
def _requested_panels(body: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(body, dict):
        raise ValueError("请求必须是对象")
    items = body.get("panels")
    if not isinstance(items, list):
        raise ValueError("panels 必须是数组")
    result = []
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("plugin_id"), str)
                or not isinstance(item.get("panel_id"), str)):
            raise ValueError("面板请求格式无效")
        result.append((item["plugin_id"], item["panel_id"]))
    return tuple(result)


# LLM: owner 用与插件命令相同的作用域解析；已加载 owner 才读活动投影，冷 owner 不为展示而加载实例。
# 函数用途: 解析插件 owner、线程键和该线程的公开活动投影。
def _owner_and_activity(base, scope) -> tuple[object, str, dict]:
    from ..conversation.agent_activity import conversation_agent_activity
    from ..conversation.store import ConversationStore
    from ..user_space.owner_resolver import resolve_owner_home
    from .control_service import resolve_gateway_scope_owner, resolve_loaded_gateway_scope_agent

    owner = resolve_owner_home(base.home_paths.root, resolve_gateway_scope_owner(base, scope))
    conversation_id = str(getattr(scope, "conversation_id", "") or "").strip()
    thread_key = "conversation:" + conversation_id
    owner_agent = resolve_loaded_gateway_scope_agent(base, scope)
    store = getattr(owner_agent, "conversation_store", None) if owner_agent is not None else None
    if not conversation_id or not isinstance(store, ConversationStore):
        return owner, thread_key, {}
    thread, _error = store.threads.resolve_report(
        channel=str(scope.channel or "").strip(),
        channel_conversation_id=conversation_id,
        channel_user_id=str(scope.user_id or "").strip(),
    )
    if thread is None:
        return owner, thread_key, {}
    thread_id = str(getattr(thread, "thread_id", "") or "")
    return owner, "thread:" + thread_id, conversation_agent_activity(owner_agent, store, thread_id).to_dict()


# LLM: 只为已加载的 owner 返回惰性提供方，冷 owner 不为展示加载实例；只读该 owner 自己的会话记录，
#   输出白名单字段（编号、时间、渠道、是否当前），不含 metadata、标题、路径或读取错误详情。
# 函数用途: 生成插件面板 sessions 主题用的会话列表读取函数，由展示服务按需调用。
def _owner_sessions(base, scope):
    from ..session.manager import SessionManager
    from .control_service import resolve_loaded_gateway_scope_agent

    owner_agent = resolve_loaded_gateway_scope_agent(base, scope)
    config = getattr(owner_agent, "config", None)
    if config is None or not getattr(config, "session_workspace", ""):
        return None
    current = str(getattr(scope, "conversation_id", "") or "")

    def read() -> list[dict]:
        sessions, _errors = SessionManager(config).list_sessions_report()
        return [{"session_id": item.session_id, "updated_at": item.updated_at, "created_at": item.created_at,
                 "channel": item.last_active_channel, "current": item.session_id == current} for item in sessions]

    return read
