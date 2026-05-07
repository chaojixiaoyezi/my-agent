# LLM: Gateway service module; keep file-queue, daemon, HTTP, and audit contracts stable.
# 模块用途: 拆分 gateway 请求队列、守护进程、HTTP 处理和响应渲染逻辑。

from __future__ import annotations

"""HTTP service for gateway using standard library http.server.

这个文件实现 gateway 的 HTTP 接口：POST /ask、GET /result/<id>、GET /status、POST /stop。
用标准库 http.server + threading 实现并发。
支持多租户鉴权：外部通道请求需要 X-User-Id / X-Channel header。
"""

import json
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Optional

from .http_handlers import (
    handle_admin_summary,
    handle_ask,
    handle_result,
    handle_session_bind,
    handle_session_channels,
    handle_status,
    handle_stop,
)

if TYPE_CHECKING:
    from ..agent.core import SimpleAgent
    from ..auth.middleware import AuthMiddleware
    from ..session.admin_query import AdminCrossChannelQuery
    from ..session.cross_channel import CrossChannelSession
    from .paths import GatewayPaths


# LLM: HTTP 启动选项先集中到参数记录，再暴露给请求处理器状态。
# Global server instance for signal handler access
_server_instance: GatewayHTTPServer | None = None


# LLM: GatewayHTTPServerParams 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 集中保存网关httpserver参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class GatewayHTTPServerParams:
    cross_channel: CrossChannelSession | None = None
    admin_query: AdminCrossChannelQuery | None = None
    auth_middleware: AuthMiddleware | None = None


# LLM: _generate_request_id 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 处理generate请求id相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _generate_request_id() -> str:
    return f"req_{int(time.time() * 1000)}_{os.getpid()}"


# LLM: GatewayHTTPHandler 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 封装网关httphandler相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发请求队列、租约文件、进程状态和响应渲染相关副作用，需保持公开契约稳定。
class GatewayHTTPHandler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    # LLM: log_message 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 写入消息的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动请求队列、租约文件、进程状态和响应渲染，调用方依赖写入顺序和文件格式。
    def log_message(self, format: str, *args: Any) -> None:
        pass

    # LLM: _inject_auth_middleware 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 处理injectauthmiddleware相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
    def _inject_auth_middleware(self) -> None:
        server = _server_instance
        if server is not None and server.auth_middleware is not None:
            self._auth_middleware = server.auth_middleware

    # LLM: _send_json 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 发送JSON请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        body_str = json.dumps(body, ensure_ascii=False)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_str.encode("utf-8"))))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body_str.encode("utf-8"))

    # LLM: _read_json 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 读取或查询JSON需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8"))

    # LLM: do_GET 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 处理doget相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def do_GET(self) -> None:
        self._inject_auth_middleware()
        if self.path == "/status":
            self._handle_status()
            return
        if self.path.startswith("/result/"):
            self._handle_result()
            return
        if self.path.startswith("/sessions/") and self.path.endswith("/channels"):
            self._handle_session_channels()
            return
        if self.path == "/admin/summary":
            self._handle_admin_summary()
            return
        self._send_json(404, {"error": "not found"})

    # LLM: do_POST 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 处理dopost相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def do_POST(self) -> None:
        self._inject_auth_middleware()
        if self.path == "/ask":
            self._handle_ask()
            return
        if self.path == "/stop":
            self._handle_stop()
            return
        if self.path.startswith("/sessions/") and self.path.endswith("/bind"):
            self._handle_session_bind()
            return
        self._send_json(404, {"error": "not found"})

    # LLM: _handle_status 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进状态的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def _handle_status(self) -> None:
        handle_status(self, _server_instance)

    # LLM: _handle_result 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进结果的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def _handle_result(self) -> None:
        handle_result(self, _server_instance)

    # LLM: _handle_ask 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进ask的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def _handle_ask(self) -> None:
        handle_ask(self, _server_instance, _generate_request_id)

    # LLM: _handle_stop 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进handlestop的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def _handle_stop(self) -> None:
        handle_stop(self, _server_instance)

    # LLM: _handle_session_channels 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进会话channels的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def _handle_session_channels(self) -> None:
        handle_session_channels(self, _server_instance)

    # LLM: _handle_session_bind 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进会话bind的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def _handle_session_bind(self) -> None:
        handle_session_bind(self, _server_instance)

    # LLM: _handle_admin_summary 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进管理summary的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def _handle_admin_summary(self) -> None:
        handle_admin_summary(self, _server_instance)


# LLM: GatewayHTTPServer 属于网关守护进程的类边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 类用途: 封装网关httpserver相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发请求队列、租约文件、进程状态和响应渲染相关副作用，需保持公开契约稳定。
class GatewayHTTPServer:

    # LLM: __init__ 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持请求队列、租约文件、进程状态和响应渲染上的返回值和副作用边界稳定。
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
        self.server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # LLM: start 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进start的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def start(self) -> None:
        global _server_instance
        _server_instance = self

        self.server = ThreadingHTTPServer(("", self.port), GatewayHTTPHandler)
        self.server.server_version = "MyAgentGateway/1.0"
        self.server.handler_class = GatewayHTTPHandler

        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # LLM: _serve 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进serve的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def _serve(self) -> None:
        if self.server is None:
            return
        try:
            self.server.serve_forever()
        except Exception:
            pass

    # LLM: stop 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
    # 函数用途: 推进stop的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响请求队列、租约文件、进程状态和响应渲染，需保持重试、超时和状态迁移语义。
    def stop(self, timeout: float = 5.0) -> None:
        global _server_instance
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        _server_instance = None


# LLM: start_http_server 属于网关守护进程的函数边界；调整时先确认请求队列、租约文件、进程状态和响应渲染仍按原契约工作。
# 函数用途: 推进HTTPserver的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
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
