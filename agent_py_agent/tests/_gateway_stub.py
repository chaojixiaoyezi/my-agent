# LLM: G3 插件命令回归：TUI 提交测试必须让真实 HTTP 传输和凭据分流跑起来；这个假 Gateway 只监听本机随机
#   端口，记录请求并回放调用方给定的响应，不接触真实 Gateway、真实凭据或用户目录。改请求协议或头时同步检查。
# 模块用途: 给 TUI 插件命令测试提供本机假 Gateway 服务、请求记录和标准插件命令响应器。

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..agent.gateway_parts.command_stream_protocol import (
    COMMAND_STREAM_MEDIA_TYPE,
    CommandStreamFrame,
)
from ..agent.plugin_command_service import execute_plugin_command


# LLM: 只保存一次假会话的路径、头和解析后正文；正文 None 表示空 body。断言 token 用 headers.get_all。
# 类用途: 表示一条已记录到假 Gateway 的请求，供测试核对路径、身份头和命令参数。
@dataclass(frozen=True)
class StubRequest:
    path: str
    headers: object
    payload: dict | None


# LLM: 响应器在服务线程执行，只能读测试传入的闭包数据；返回 (body bytes, media type)，本类不做业务判断。
# 类用途: 起停一个本机随机端口的假 Gateway，按响应器回放请求并记录。
class GatewayStub:
    def __init__(self, responder) -> None:
        self._responder = responder
        self.requests: list[StubRequest] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._server.server_port
        self._worker = threading.Thread(target=self._server.serve_forever, daemon=True)

    # LLM: 处理器只做协议搬运：读 body、记录、调用响应器、写回 200；响应器异常照常冒泡进测试输出。
    # 函数用途: 构造 BaseHTTPRequestHandler 子类，把请求转交给响应器。
    def _make_handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                payload = json.loads(data) if data else None
                stub.requests.append(StubRequest(self.path, self.headers, payload))
                body, media_type = stub._responder(self.path, self.headers, payload)
                self.send_response(200)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _reply
            do_POST = _reply

            def log_message(self, *_args):
                pass

        return Handler

    # LLM: start 只启动后台线程；stop 有界回收，测试结束必须调用（fixture 会统一回收）。
    # 函数用途: 启动/停止监听。
    def start(self) -> GatewayStub:
        self._worker.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._worker.join(2)


# LLM: 生命周期由 with 管理，异常路径也保证关掉监听；端口 0 由系统分配，避免并发测试撞车。
# 函数用途: 在 with 块里提供一个运行中的假 Gateway。
@contextmanager
def gateway_stub(responder):
    stub = GatewayStub(responder).start()
    try:
        yield stub
    finally:
        stub.stop()


# LLM: 响应语义与真实客户端合同一致：catalog 回目录、command 回执行结果、interactive 回 NDJSON 帧；
#   目录每次现取（闭包），测试中途替换目录对象也能被看到。
# 函数用途: 生成插件命令测试的标准假 Gateway 响应器；catalog_provider 返回当前 PluginCommandCatalog。
def plugin_command_responder(catalog_provider):
    def respond(path, headers, payload):
        catalog = catalog_provider()
        if payload and payload.get("interactive"):
            request_id = payload["plugin_request_id"]
            result = execute_plugin_command(
                catalog, payload["command"], revision=payload["catalog_revision"]
            )
            frames = CommandStreamFrame(
                request_id,
                "connected",
                {"owner": {"provider": "local", "owner_kind": "main", "owner_id": "main"}},
            ).encode()
            frames += CommandStreamFrame(request_id, "result", result).encode()
            return frames, COMMAND_STREAM_MEDIA_TYPE
        if payload["operation"] == "catalog":
            body = {"ok": True, "catalog": catalog.to_payload()}
        else:
            body = execute_plugin_command(
                catalog, payload["command"], revision=payload["catalog_revision"]
            )
        return json.dumps(body).encode(), "application/json"

    return respond
