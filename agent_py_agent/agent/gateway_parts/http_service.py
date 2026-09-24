
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
import itertools
import json
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
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
    handle_client_goal,
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

    def do_GET(self) -> None:
        self._inject_auth_middleware()
        if self.path == "/status":
            self._handle_status()
            return
        if self.path.startswith("/result/"):
            self._handle_result()
            return
        if self.path.startswith("/input-status/"):
            self._handle_input_status()
            return
        if self.path.startswith("/control-status/"):
            self._handle_control_status()
            return
        if self.path.startswith("/progress/"):
            # `/progress` 只读 typed event，并在 handler 内复用 `/result` 的 owner 权限事实。
            self._handle_progress()
            return
        if self.path.startswith("/sessions/") and self.path.endswith("/channels"):
            self._handle_session_channels()
            return
        if self.path == "/admin/summary":
            self._handle_admin_summary()
            return
        if self.path == "/metrics":
            self._handle_metrics()
            return
        self._send_json(404, {"error": "not found"})

    # LLM: 各控制、插件目录和显示入口复用认证中间件；插件声明不进任务队列，权限与 owner 仍由服务端核验。
    # 函数用途: 将 HTTP POST 分发到对应服务，原文页也必须校验用户和会话，不从正文推断权限。
    def do_POST(self) -> None:
        self._inject_auth_middleware()
        if self.path == "/ask":
            self._handle_ask()
            return
        if self.path == "/control":
            self._handle_control()
            return
        if self.path == "/client/memory":
            self._handle_client_memory()
            return
        if self.path == "/client/models":
            from .model_profile_service import handle_client_models

            handle_client_models(self, _server_instance)
            return
        if self.path == "/client/plugins":
            from .plugin_command_service import handle_client_plugins

            handle_client_plugins(self, _server_instance)
            return
        if self.path == "/client/plugin-panels":
            from .plugin_panels_http import handle_client_plugin_panels

            handle_client_plugin_panels(self, _server_instance)
            return
        if self.path == "/client/permissions":
            from .approval_mode_service import handle_client_approval_mode

            handle_client_approval_mode(self, _server_instance)
            return
        if self.path == "/client/history":
            self._handle_client_history()
            return
        if self.path == "/client/display-page":
            from .display_archive_service import handle_client_display_page

            handle_client_display_page(self, _server_instance)
            return
        if self.path == "/client/notices":
            self._handle_client_notices()
            return
        if self.path == "/client/agent-view":
            self._handle_client_agent_view()
            return
        if self.path == "/client/goal":
            handle_client_goal(self, _server_instance)
            return
        if self.path == "/client/agent-guidance":
            self._handle_client_agent_guidance()
            return
        if self.path == "/client/agent-permission":
            self._handle_client_agent_permission()
            return
        if self.path == "/client/agent-stop":
            self._handle_client_agent_stop()
            return
        if self.path == "/stop":
            self._handle_stop()
            return
        if self.path.startswith("/sessions/") and self.path.endswith("/bind"):
            self._handle_session_bind()
            return
        self._send_json(404, {"error": "not found"})

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
        # 插件展示服务由首个面板请求惰性创建（见 plugin_panels_http），停止时一并关闭
        self.plugin_display = None

    def _guard_network_exposure(self) -> None:
        """fail-closed:绑非 loopback(暴露到网络)却没接鉴权中间件时拒绝启动,杜绝未认证远程入口。"""
        if not _is_loopback_host(self.bind_host) and self.auth_middleware is None:
            raise RuntimeError(
                f"网关拒绝启动:bind_host={self.bind_host!r} 非回环(暴露到网络)却未配置鉴权。"
                "请置 auth_enabled=True,或把 gateway_bind_host 设回 127.0.0.1。"
            )

    # LLM: start() is the only socket construction point and must instantiate the Gateway-specific
    # concurrent server so every caller receives the same backlog and shutdown semantics.
    # 函数用途: 通过网络暴露检查后启动唯一 Gateway HTTP 服务线程。
    def start(self) -> None:
        self._guard_network_exposure()  # fail-closed 必须先于任何全局副作用(拒绝时不污染 _server_instance)
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
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self.plugin_display is not None:
            self.plugin_display.close()
            self.plugin_display = None
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
