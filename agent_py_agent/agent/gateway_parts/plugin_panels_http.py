# LLM: /client/plugin-panels 入口：可信来源检查先于读正文；owner 与线程只由 Gateway 作用域解析，客户端只能选择面板。
#   活动投影复用 conversation_agent_activity 的同一只读结果，冷 owner 用空投影；不写任何状态，不加载 owner 实例。
# 模块用途: 把 TUI 打开的插件面板请求交给进程内唯一的展示服务，返回已校验的面板内容。
from __future__ import annotations

import logging
import threading
from functools import partial

from ..plugin_channel import PluginChannelRevoked

logger = logging.getLogger(__name__)

_SERVICE_LOCK = threading.Lock()


# LLM: 停机是终态：server_close() 不等正在跑的 HTTP 工作线程，晚到的面板请求或事件中心取池
#   会在停机后重新建出一个打开的新池，它拉起的插件进程之后再也没人关。所以 stop 必须在
#   _SERVICE_LOCK 内先置这个标记，之后一切取池/取服务都拒绝新建。
# 函数用途: 判断该 server 的插件通道是否已随 Gateway 停机关闭。
def _channel_closed(server) -> bool:
    return bool(getattr(server, "plugin_channel_closed", False))


# LLM: 只由 Gateway stop 调用；必须在 _SERVICE_LOCK 内调用，保证与取池/取服务互斥，
#   不会出现"检查时还没关、关的时候又建了一个"的交错。
# 函数用途: 标记该 server 的插件通道已关闭（调用方已持 _SERVICE_LOCK）。
def mark_plugin_channel_closed(server) -> None:
    server.plugin_channel_closed = True


# LLM: 停机是终态，也是幂等的：置标记与摘掉服务/池引用必须在同一把锁内做完，否则锁外关池的瞬间
#   还在跑的工作线程又能取到旧引用或新建一个。真正的 close 放到锁外做——它会 stop 插件进程，
#   可能阻塞，不能占着 _SERVICE_LOCK（那时新的取池请求会被标记挡住，不会漏）。
#   事件中心与展示服务一起摘掉：两者共用同一个池，停机后都不允许再被取到。
# 函数用途: 关闭本 server 的插件展示服务、事件中心与共用池，并标记通道已关闭（可重复调用）。
def close_plugin_channel(server) -> None:
    with _SERVICE_LOCK:
        mark_plugin_channel_closed(server)
        service = getattr(server, "plugin_display", None)
        server.plugin_display = None
        hub = getattr(server, "plugin_event_hub", None)
        server.plugin_event_hub = None
        pool = getattr(server, "plugin_channel_pool", None)
        server.plugin_channel_pool = None
    if service is not None:
        service.close()
    if hub is not None:
        hub.close()
    if pool is not None:
        pool.close()


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
    try:
        service = plugin_display_service(server)
        panels = service.panels(PanelQuery(owner, str(owner.home_dir), thread_key, activity, requested, sessions))
    except PluginChannelRevoked:
        # 停机后晚到的面板请求：不新建池、不起进程，按关闭返回空面板而不是 500
        handler._send_json(200, {"ok": False, "panels": [], "error": "插件通道已关闭"})
        return
    handler._send_json(200, {"ok": True, "panels": panels})


# LLM: 池挂在唯一 HTTP server 上，面板服务和以后的事件中心共用同一条（owner, 激活代次）连接；
#   服务由 Gateway 在停止时统一关闭，stop 负责关池（池由这里创建，生命周期归 server）。
#   本函数只在 _SERVICE_LOCK 之外被调用，内部自己拿锁，避免与 plugin_display_service 重入同一把锁。
#   停机后（server 上已置关闭标记）拒绝新建：那时还在跑的工作线程不能悄悄再建一个没人关的池。
# 函数用途: 取得（必要时创建）本 Gateway 进程的插件展示服务。
def plugin_display_service(server):
    from ..plugin_display.service import DisplayWiring, PluginDisplayService

    with _SERVICE_LOCK:
        if _channel_closed(server):
            raise PluginChannelRevoked("插件通道已随 Gateway 停机关闭")
        service = getattr(server, "plugin_display", None)
        if service is None:
            # 面板连接与业务连接同一沙箱开关（配置 plugin_process_sandbox）
            sandbox = bool(getattr(getattr(server.agent, "config", None), "plugin_process_sandbox", False))
            # 直接复用取/建逻辑，不能再取 _SERVICE_LOCK（本函数已持有，非可重入锁会死锁）
            pool = _locked_shared_pool(server, sandbox)
            server.plugin_channel_pool = pool
            service = PluginDisplayService(wiring=DisplayWiring(pool=pool))
            server.plugin_display = service
        return service


# LLM: 一个 Gateway 进程只建一个池；面板服务和事件中心都从这里取，不能再各建一个。
#   池一旦建好就固定创建客户端用的沙箱开关，避免同一进程里出现两种插件进程形态。
#   停机后拒绝新建（同 plugin_display_service）：以后事件中心在停机排空时取池也会走到这里。
# 函数用途: 取得（必要时创建）挂在 server 上的共用插件通道池。
def plugin_channel_pool(server, process_sandbox: bool | None = None):
    with _SERVICE_LOCK:
        return _locked_shared_pool(server, process_sandbox)


# LLM: 事件中心与面板服务共用 server 上的唯一池（不建第二个池）；hub 只在第一次取用时创建，
#   之后原样返回。停机后拒绝新建：停机排空阶段的事件点发布按丢弃处理，不重试、不拉起进程。
# 函数用途: 取得（必要时创建）本 Gateway 进程的插件事件中心。
def plugin_event_hub(server):
    from ..plugin_events.hub import EventHubWiring, PluginEventHub

    with _SERVICE_LOCK:
        if _channel_closed(server):
            raise PluginChannelRevoked("插件通道已随 Gateway 停机关闭")
        hub = getattr(server, "plugin_event_hub", None)
        if hub is None:
            sandbox = bool(getattr(getattr(server.agent, "config", None), "plugin_process_sandbox", False))
            # 直接复用取/建逻辑，不能再取 _SERVICE_LOCK（本函数已持有，非可重入锁会死锁）
            pool = _locked_shared_pool(server, sandbox)
            server.plugin_channel_pool = pool
            hub = PluginEventHub(wiring=EventHubWiring(pool=pool))
            server.plugin_event_hub = hub
        return hub


# LLM: 事件点的统一入口：拿不到 hub（停机、未初始化）就丢弃这条事件，绝不把异常抛回主流程；
#   第一次取用要新建 hub 和池（导入、读配置、构造对象），这里任何异常都不能漏出去——
#   B4 在主流程的事件点直接调本函数。拿到 hub 之后 hub.publish 本身也保证不抛。
# 函数用途: 向本 Gateway 的事件中心发布一条观察事件（永不抛异常）。
def publish_plugin_event(server, owner, event) -> None:
    try:
        hub = plugin_event_hub(server)
    except PluginChannelRevoked:
        return
    except Exception:  # noqa: BLE001 取用失败不能把异常抛回主流程的事件点
        logger.warning("插件事件中心取用失败，已丢弃事件", exc_info=True)
        return
    hub.publish(owner, event)


# LLM: 组合根/工具执行不持有第二个 server 或连接池；只解析 HTTP 服务已存在的唯一实例，停机仍沿原终态。
# 函数用途: 为 B5 提供当前 Gateway 共用池；不经 Gateway 的场合返回无通道，匹配插件宁严要求确认。
def current_plugin_channel_pool():
    from .http_service import _server_instance

    return plugin_channel_pool(_server_instance) if _server_instance is not None else None


# LLM: 只在 _SERVICE_LOCK 内调用；已存在的池原样返回，不存在才按沙箱开关新建并挂上 server。
#   已关闭的 server 一律拒绝：池是进程级共享资源，停机后再建一个就没人在关它了。
# 函数用途: 取或建 server 上的唯一池（调用方已持锁）。
def _locked_shared_pool(server, process_sandbox: bool | None):
    from ..plugin_channel import PluginChannelPool
    from ..plugin_display.service import plugin_display_client

    if _channel_closed(server):
        raise PluginChannelRevoked("插件通道已随 Gateway 停机关闭")
    pool = getattr(server, "plugin_channel_pool", None)
    if pool is None:
        if process_sandbox is None:
            config = getattr(getattr(server, "agent", None), "config", None)
            process_sandbox = bool(getattr(config, "plugin_process_sandbox", False))
        pool = PluginChannelPool(client_factory=partial(plugin_display_client, process_sandbox=process_sandbox))
        server.plugin_channel_pool = pool
    return pool


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
