# LLM: HTTP传输沿唯一结构化分发表，观察复用同一路由模板；启动先确保/绑定持久凭据，秘密不进状态、日志或环境。
# 模块用途: 提供有界 Gateway HTTP 监听器与宿主客户端入口。
from __future__ import annotations

"""HTTP service for gateway using a bounded standard-library HTTP server.

这个文件实现 gateway 的 HTTP 接口：POST /ask、POST /control、GET /control-status/<id>、
GET /result/<id>、GET /progress/<id>、GET /status、POST /stop、POST /client/models（私有模型配置，不入聊天队列）。
用标准库 http.server + 固定 daemon worker 池实现有界并发。
支持多租户鉴权：外部通道请求需要 X-User-Id / X-Channel header。
"""

# 路由导入按对外 HTTP 分组顺序排列，保持和下方分派表一致。
# ruff: noqa: I001

import errno
import importlib
import itertools
import json
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ..runtime_errors import runtime_error_report
from .bounded_http_server import GatewayBoundedHTTPServer
from .http_handlers import (
    handle_admin_summary,
    handle_ask,
    handle_client_history,
    handle_client_notices,
    handle_client_memory,
    handle_client_agent_guidance,
    handle_client_agent_permission,
    handle_client_agent_stop,
    handle_client_agent_view,
    handle_control,
    handle_control_status,
    handle_input_status,
    handle_progress,
    handle_result,
    handle_session_bind,
    handle_session_channels,
    handle_status,
    handle_stop,
)
from .io import update_json_file_atomic
from .local_client_token import LocalClientCredentialError, ensure_local_client_credential
from .http_routes import GATEWAY_HTTP_ROUTES as GATEWAY_HTTP_ROUTES, match_gateway_http_route
from ..path_access_policy import agent_home_root_for_owner

if TYPE_CHECKING:
    from ..agent.core import SimpleAgent
    from ..auth.middleware import AuthMiddleware
    from ..session.admin_query import AdminCrossChannelQuery
    from ..session.cross_channel import CrossChannelSession
    from .paths import GatewayPaths


# Global server instance for signal handler access
_server_instance: GatewayHTTPServer | None = None


@dataclass(frozen=True)
class GatewayHTTPServerParams:
    cross_channel: CrossChannelSession | None = None
    admin_query: AdminCrossChannelQuery | None = None
    auth_middleware: AuthMiddleware | None = None
    bind_host: str = "127.0.0.1"  # 默认仅本机可达;暴露到网络须配鉴权(见 _guard_network_exposure)
    agent: SimpleAgent | None = None
    # G2b 上线闸：true 时本机凭据不可用拒绝启动（fail-closed），由 gateway_process 从与客户端同一份配置读入。
    require_local_credential: bool = False


# 回环地址:仅本机可达。空串/0.0.0.0/::/LAN IP 一律判非回环(=暴露到网络,须鉴权)。默认仅监听回环地址。
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"}


_CLIENT_DISCONNECT_ERRNOS = {
    errno.EPIPE,
    errno.ECONNABORTED,
    errno.ECONNRESET,
}


def _is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower().strip("[]")
    return h in _LOOPBACK_HOSTS or h.startswith("127.")


# LLM: Only socket-close errno values qualify as routine client departure.
# Do not broaden this to all OSError: disk, encoding and business I/O failures
# must still reach the bounded server error path with their traceback.
# 函数用途: 判断一次 HTTP 写回失败是否只是客户端已提前关闭连接。
def _is_client_disconnect_error(exc: OSError) -> bool:
    return isinstance(
        exc,
        (BrokenPipeError, ConnectionAbortedError, ConnectionResetError),
    ) or exc.errno in _CLIENT_DISCONNECT_ERRNOS


# 进程内单调计数器:ThreadingHTTPServer 下并发请求跑在同进程多线程,同毫秒同 pid 会撞出相同
# request_id,两请求写进同一个队列文件→文件被拼成两段 JSON→读取时 "Extra data" 解析失败→
# GATEWAY_REQUEST_LOAD_ERROR,请求整个挂掉(冷启动并发实测 ~1-2/10)。加计数器保证同进程内唯一;
# next() 在 CPython 是 GIL 原子操作,ThreadingHTTPServer(线程非进程)下线程安全。
_REQUEST_ID_COUNTER = itertools.count()


def _generate_request_id() -> str:
    return f"req_{int(time.time() * 1000)}_{os.getpid()}_{next(_REQUEST_ID_COUNTER)}"


# LLM: 分发表同时决定处理器和观察模板；原业务授权不变，无中间件档不观察，不含G2b强制。
# 类用途: 接收HTTP请求并交给原业务入口，顺带观察需迁移的旧客户端。
class GatewayHTTPHandler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _inject_auth_middleware(self) -> None:
        server = _server_instance
        if server is not None and server.auth_middleware is not None:
            self._auth_middleware = server.auth_middleware

    # LLM: The socket write boundary treats only explicit disconnect errno as a
    # completed transport handoff. Header/body writes share this one boundary so
    # a departing poll client never becomes a product failure or traceback storm.
    # 函数用途: 发送短连接响应；客户端已离开时安静收口，其他 I/O 错误继续抛出。
    def _send_payload(self, status: int, content_type: str, body: bytes) -> None:
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except OSError as exc:
            if _is_client_disconnect_error(exc):
                return
            raise

    # LLM: JSON encoding remains outside the disconnect catcher so serialization
    # bugs cannot be mislabeled as a client departure.
    # 函数用途: 编码并发送 JSON，统一复用短连接写回和断连收口语义。
    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._send_payload(status, "application/json; charset=utf-8", payload)

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8", "replace"))

    # LLM: GET/POST共用实际分发表，未知路径404，公开/插件档不计迁移流量。
    # 函数用途: 沿结构化分发表分发只读HTTP请求。
    def do_GET(self) -> None:
        self._dispatch_route("GET")

    # LLM: 只改变组织与观察，原业务授权/owner/副作用仍在各处理器；不新增G2b强制。
    # 函数用途: 分发HTTP写入口并只观察旧客户端流量。
    def do_POST(self) -> None:
        self._dispatch_route("POST")

    # LLM: 同一个route既决定处理器又提供有界观察键，每请求只观察一次；惰性导入保留旧服务加载时机。
    # 函数用途: 匹配实际路由，记录无凭据回环观察并调用原处理入口。
    def _dispatch_route(self, method: str) -> None:
        self._inject_auth_middleware()
        route = match_gateway_http_route(method, self.path)
        if route is None:
            self._send_json(404, {"error": "not found"})
            return
        middleware = getattr(self, "_auth_middleware", None)
        if middleware is not None:
            middleware.observe_loopback_request(self, route)
        if route.handler_module:
            module = importlib.import_module(route.handler_module, __package__)
            getattr(module, route.handler_name)(self, _server_instance)
            return
        getattr(self, route.handler_name)()

    def _handle_status(self) -> None:
        handle_status(self, _server_instance)

    # LLM: Metrics uses the same short-connection boundary as JSON routes so
    # a scraper cannot retain one of the finite Gateway request workers.
    # 函数用途: 返回 Prometheus 指标并立即释放当前 HTTP 工作线程。
    def _handle_metrics(self) -> None:
        # §6-A 量化端点:Prometheus 文本暴露(LLM RED/token/cost + 并发占用探针同端点)。
        # 只读、无副作用;渲染失败不崩网关。
        try:
            from ..observability.concurrency_metrics import ensure_concurrency_metrics_registered
            from ..observability.metrics import default_registry

            # 并发探针是首次埋点才懒注册;重启后无流量时注册表为空,"没部署"和"没流量"
            # 分不清——渲染前主动注册,5 个系列恒以 0 值可见,scrape 侧可稳定 grep 指标名。
            ensure_concurrency_metrics_registered()
            body = default_registry().render().encode("utf-8")
        except Exception as exc:
            self._send_json(500, {"error": runtime_error_report(exc, context="gateway.metrics.render")})
            return
        self._send_payload(200, "text/plain; version=0.0.4; charset=utf-8", body)

    def _handle_result(self) -> None:
        handle_result(self, _server_instance)

    # LLM: Thin clients poll the durable ingress receipt separately from task results so an active
    # input consumed by the current turn can close without inventing a second response job.
    # 函数用途: 转发普通消息投递状态查询。
    def _handle_input_status(self) -> None:
        handle_input_status(self, _server_instance)

    # LLM: Control operation polling is separate from task results and active input receipts.
    # 函数用途: 转发持久控制操作状态查询。
    def _handle_control_status(self) -> None:
        handle_control_status(self, _server_instance)

    def _handle_progress(self) -> None:
        handle_progress(self, _server_instance)

    def _handle_ask(self) -> None:
        handle_ask(self, _server_instance, _generate_request_id)

    def _handle_control(self) -> None:
        handle_control(self, _server_instance)

    # LLM: HTTP handler only forwards authenticated JSON to the Gateway client service; it never reads memory files itself.
    # 函数用途: 处理薄客户端的记忆查询和显式保存请求。
    def _handle_client_memory(self) -> None:
        handle_client_memory(self, _server_instance)

    # LLM: HTTP handler returns owner-scoped paired history and cannot expose raw transcript paths.
    # 函数用途: 处理薄客户端的会话历史恢复请求。
    def _handle_client_history(self) -> None:
        handle_client_history(self, _server_instance)

    def _handle_client_notices(self) -> None:
        handle_client_notices(self, _server_instance)

    # LLM: HTTP routing delegates exact descendant reads to the shared owner-scoped
    # service; the handler never loads a task file directly.
    # 函数用途: 处理 TUI/Web 查看一个子代理详情的请求。
    def _handle_client_agent_view(self) -> None:
        handle_client_agent_view(self, _server_instance)

    # LLM: Guidance routing carries a stable client message id and cannot fall
    # back to /ask or create a new main-agent turn.
    # 函数用途: 处理用户给当前子代理插入补充要求的请求。
    def _handle_client_agent_guidance(self) -> None:
        handle_client_agent_guidance(self, _server_instance)

    # LLM: Approval routing accepts an exact child-published request plus one
    # typed decision; it cannot fall back to guidance text or main-turn input.
    # 函数用途: 处理用户对当前任务树内子代理工具调用的批准或拒绝。
    def _handle_client_agent_permission(self) -> None:
        handle_client_agent_permission(self, _server_instance)

    # LLM: Stop routing calls only the canonical child cancellation service;
    # it never maps to process names or UI row positions.
    # 函数用途: 处理用户按 Esc 停止当前子代理的请求。
    def _handle_client_agent_stop(self) -> None:
        handle_client_agent_stop(self, _server_instance)

    def _handle_stop(self) -> None:
        handle_stop(self, _server_instance)

    def _handle_session_channels(self) -> None:
        handle_session_channels(self, _server_instance)

    def _handle_session_bind(self) -> None:
        handle_session_bind(self, _server_instance)

    def _handle_admin_summary(self) -> None:
        handle_admin_summary(self, _server_instance)


# LLM: G2b 强制阶段凭据不可用时拒绝启动的结构化错误；reason_code 是机器合同（G1 原因码，含 LOCAL_CLIENT_CREDENTIAL_NO_DATA_ROOT），
#   消息不含路径、内容或凭据，调用方（gateway_process 启动链）据此给出可诊断的失败。
# 类用途: 表示"已开启强制开关但本机凭据不可用"，让宿主启动 fail-closed。
class GatewayLocalCredentialRequired(RuntimeError):
    # LLM: 只接受内部生成的原因码；不包装底层异常，避免路径或内容进入错误链。
    # 函数用途: 构造只含原因码的启动拒绝错误。
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"网关拒绝启动: 已开启 gateway_require_local_credential，但本机客户端凭据不可用（{reason_code}）")


# LLM: G2b 启动预检与 GatewayHTTPServer.start() 必须用同一份"服务端准备凭据"逻辑：缺失时生成、坏文件不改写、
#   推不出数据根或合同不符时抛结构化原因码。预检先用它，才能既在副作用之前拒绝，又不把"缺了就生成"误判成失败。
#   不看 gateway_auth_token（那是部署 token，不是本机凭据）；ensure 幂等，预检与 start() 各调一次没问题。
# 函数用途: 按真实 Agent 的 home 合同准备本机凭据，返回凭据内容或抛出结构化原因码。
def prepare_local_client_credential(agent: object) -> str:
    root = local_credential_data_root(agent)
    if root is None:
        raise LocalClientCredentialError("LOCAL_CLIENT_CREDENTIAL_NO_DATA_ROOT")
    return ensure_local_client_credential(root)


# LLM: Agent.home_paths 是 SimpleAgent 必有合同；其 owner_home_dir 经插件沙箱同一 helper 推根，并校验 root 投影一致。
#   agent=None 表示没有 canonical home 权威，不能从队列位置推测写入目标。
# 函数用途: 从可信 Agent 的真实 home 路径解析凭据数据根，合同错误抛原因码，缺少 Agent 根时返回 None。
def local_credential_data_root(agent: object) -> Path | None:
    if agent is None:
        return None
    try:
        home_paths = agent.home_paths
        owner_home = Path(home_paths.owner_home_dir)
        declared_root = Path(home_paths.root).expanduser().resolve()
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        raise LocalClientCredentialError("agent_contract") from None
    data_root = agent_home_root_for_owner(owner_home)
    if data_root is None:
        return None
    if declared_root != data_root:
        raise LocalClientCredentialError("agent_contract")
    return data_root


# LLM: This wrapper owns the lifecycle of exactly one concurrent HTTP listener. Changes must keep
# exposure checks before bind, preserve the module-global handler context, and stop cleanly without
# changing request/owner serialization inside Gateway services.
# 类用途: 启动、记录故障并关闭 Gateway 的唯一 HTTP 监听器。
class GatewayHTTPServer:

    # LLM: Construction stores dependencies only; it must not bind a socket or mutate the global
    # server pointer before start() passes the network exposure guard.
    # 函数用途: 保存端口、路径、鉴权和 Agent 依赖，等待显式 start 启动监听。
    def __init__(
        self,
        port: int,
        paths: GatewayPaths,
        *,
        params: GatewayHTTPServerParams | None = None,
        cross_channel: CrossChannelSession | None = None,
        admin_query: AdminCrossChannelQuery | None = None,
        auth_middleware: AuthMiddleware | None = None,
    ):
        server_params = params or GatewayHTTPServerParams(cross_channel, admin_query, auth_middleware)
        self.port = port
        self.paths = paths
        self.cross_channel = server_params.cross_channel
        self.admin_query = server_params.admin_query
        self.auth_middleware = server_params.auth_middleware
        self.bind_host = server_params.bind_host
        self.agent = server_params.agent
        self.server: GatewayBoundedHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.last_error_report: dict[str, Any] | None = None
        # G2b 开关：true 时凭据不可用拒绝启动；false（G2a）只降级为状态。状态串只给 ok / unavailable:原因码，不给路径或内容。
        self.require_local_credential = bool(server_params.require_local_credential)
        self.local_credential_status = "unavailable:not_prepared"
        self._local_client_credential = ""
        # 插件展示服务与共用插件通道池都由首个面板请求惰性创建（见 plugin_panels_http），停止时一并关闭
        self.plugin_display = None
        self.plugin_channel_pool = None

    def _guard_network_exposure(self) -> None:
        """fail-closed:绑非 loopback(暴露到网络)却没接鉴权中间件时拒绝启动,杜绝未认证远程入口。"""
        if not _is_loopback_host(self.bind_host) and self.auth_middleware is None:
            raise RuntimeError(
                f"网关拒绝启动:bind_host={self.bind_host!r} 非回环(暴露到网络)却未配置鉴权。"
                "请置 auth_enabled=True,或把 gateway_bind_host 设回 127.0.0.1。"
            )

    # LLM: 唯一监听启动点，暴露检查先于 bind/全局指针；G2a 档凭据准备失败降级为状态不拦启动，G2b 开关打开时 fail-closed 拒绝启动，
    #   拒绝发生在设置全局指针与 bind 之前；停机不删凭据。
    # 函数用途: 检查暴露边界、准备本机凭据状态后启动 HTTP 服务线程。
    def start(self) -> None:
        self._guard_network_exposure()  # fail-closed 必须先于任何全局副作用(拒绝时不污染 _server_instance)
        self._prepare_local_credential()
        global _server_instance
        _server_instance = self
        self.last_error_report = None

        self.server = GatewayBoundedHTTPServer(
            (self.bind_host, self.port),
            GatewayHTTPHandler,
        )
        self.server.server_version = "MyAgentGateway/1.0"
        self.server.handler_class = GatewayHTTPHandler

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        # 插件宿主只读 API 只在本服务运行时可用，地址固定为回环
        from ..plugin_host_api import set_host_api_base

        bound_port = self.server.server_address[1]
        set_host_api_base(f"http://127.0.0.1:{bound_port}")
        # G4（Gateway 本机信任第 (1) 层）：登记实际绑定端口，模型命令沙箱据此按端口拒绝回环连接。
        # 用实际绑定端口而非配置值，覆盖 --port 覆写和测试随机端口。
        from ..attempt.sandbox import register_gateway_bound_port

        register_gateway_bound_port(bound_port)

    # LLM: 数据根只从真实 Agent.home_paths.owner_home_dir 经共享 canonical 推导取得；缺失属性是合同错误，None Agent 不授权队列路径写凭据。
    #   凭据准备失败走 _degrade_or_refuse：G2a 档只降级为结构化状态、不拦启动；G2b 强制档拒绝启动。凭据不进入状态或 env。
    # 函数用途: 准备持久本机客户端凭据，失败时降级记录或按开关拒绝启动。
    def _prepare_local_credential(self) -> None:
        try:
            credential = prepare_local_client_credential(self.agent)
        except LocalClientCredentialError as exc:
            self._degrade_or_refuse(exc.reason_code)
            return
        self._apply_local_credential(credential, "ok")

    # LLM: 强制档（开关打开）凭据不可用必须 fail-closed：抛结构化错误，调用方拒绝启动且不 bind 端口；
    #   迁移档只把原因写成公开降级状态（unavailable:原因码），照常启动，不覆盖坏文件、不轮换。
    # 函数用途: 按当前档位处理"凭据不可用"——降级记状态或拒绝启动。
    def _degrade_or_refuse(self, reason_code: str) -> None:
        self._apply_local_credential("", f"unavailable:{reason_code}")
        if self.require_local_credential:
            raise GatewayLocalCredentialRequired(reason_code)

    # LLM: 凭据值、公开状态与中间件绑定必须同步更新；失败时清空绑定，避免上一轮或坏文件残留可用凭据的错觉。
    # 函数用途: 写入凭据与状态，并同步给鉴权中间件。
    def _apply_local_credential(self, credential: str, status: str) -> None:
        self._local_client_credential = credential
        self.local_credential_status = status
        if self.auth_middleware is not None:
            self.auth_middleware.set_local_client_credential(credential)

    def _serve(self) -> None:
        if self.server is None:
            return
        try:
            self.server.serve_forever()
        except Exception as exc:
            self._record_serve_error(exc)

    def _record_serve_error(self, exc: BaseException) -> None:
        report = runtime_error_report(exc, context="gateway.http_server.serve")
        self.last_error_report = report

        def update_state(current: dict) -> dict:
            updated = dict(current)
            updated["status"] = "http_server_error"
            updated["server_error"] = report
            updated["updated_at"] = time.time()
            return updated

        try:
            update_json_file_atomic(self.paths.state, update_state)
        except Exception as persist_exc:
            self.last_error_report = {
                **report,
                "state_persist_error": runtime_error_report(
                    persist_exc,
                    context="gateway.http_server.state.write",
                ),
            }

    def stop(self, timeout: float = 5.0) -> None:
        global _server_instance
        from ..plugin_host_api import set_host_api_base
        from .plugin_panels_http import close_plugin_channel

        set_host_api_base(None)
        # G4：停机时注销本服务登记的 Gateway 端口（在 server_close 前取端口）。
        if self.server:
            from ..attempt.sandbox import unregister_gateway_bound_port

            unregister_gateway_bound_port(self.server.server_address[1])
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        # server_close() 不等还在跑的工作线程：停机后晚到的面板请求或事件中心取池会重新建一个
        # 没人关的池。close_plugin_channel 在 _SERVICE_LOCK 内先置"已关闭"标记再摘引用，
        # 之后的取池/取服务一律被拒。
        close_plugin_channel(self)
        _server_instance = None


def start_http_server(
    port: int,
    paths: GatewayPaths,
    *,
    params: GatewayHTTPServerParams | None = None,
    cross_channel: CrossChannelSession | None = None,
    admin_query: AdminCrossChannelQuery | None = None,
    auth_middleware: AuthMiddleware | None = None,
) -> GatewayHTTPServer:
    server = GatewayHTTPServer(
        port,
        paths,
        params=params or GatewayHTTPServerParams(cross_channel, admin_query, auth_middleware),
    )
    server.start()
    return server
