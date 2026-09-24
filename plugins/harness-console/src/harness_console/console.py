# LLM: 本模块在插件进程内起标准库 ThreadingHTTPServer：只绑定 127.0.0.1 随机端口，只接受 GET（HEAD 与写方法一律拒绝）。
#   每次启动生成新的随机会话令牌：首页需查询参数或 cookie 令牌，首次带令牌访问下发 HttpOnly+SameSite=Strict cookie；
#   /api/state 只认 cookie，由服务端调宿主 API（宿主令牌只在服务端）后返回白名单 JSON。
#   副作用：占用一个本地端口、启动服务线程和空闲看守线程（均为 daemon）；不写访问日志，避免会话令牌进入宿主日志。
# 模块用途: 实现工作台网页服务的完整生命周期（启动、鉴权、路由、空闲自停、关闭释放端口）。

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .host import HostClient, console_state
from .page import console_page

HOST = "127.0.0.1"
PAGE_POLICY = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
               "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


# LLM: 服务状态全部在实例内，由 lock 保护；close 幂等且持锁完成，返回时端口已释放；再次启动必须新建实例。
# 类用途: 表示一次启动的工作台网页服务。
class ConsoleService:
    # LLM: 构造即绑定端口并启动两个 daemon 线程；host 客户端由调用方传入，服务只读它。
    # 函数用途: 生成会话令牌、绑定随机回环端口并开始提供服务。
    def __init__(self, host: HostClient, *, idle_stop_seconds: int, poll_seconds: int):
        self.host, self.idle_stop_seconds, self.poll_seconds = host, idle_stop_seconds, poll_seconds
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.requests, self.last_active = 0, time.monotonic()
        self.stop_reason: str | None = None
        self.stopped = threading.Event()
        self.httpd = ThreadingHTTPServer((HOST, 0), ConsoleHandler)
        self.httpd.console = self
        self.port = self.httpd.server_address[1]
        self.cookie_name = f"harness_console_{self.port}"
        threading.Thread(target=self.httpd.serve_forever, name="harness-console-http", daemon=True).start()
        threading.Thread(target=self.watch_idle, name="harness-console-idle", daemon=True).start()

    # 函数用途: 返回带会话令牌的完整访问地址（只写私有文件或交给浏览器，不进工具结果）。
    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/?token={self.token}"

    # 函数用途: 服务是否仍在运行。
    @property
    def running(self) -> bool:
        with self.lock:
            return self.stop_reason is None

    # LLM: 只读快照；结果进入模型上下文，只给不含令牌的地址。
    # 函数用途: 汇报是否在服务、地址、端口和已处理请求数。
    def snapshot(self) -> dict:
        with self.lock:
            running = self.stop_reason is None
            return {"serving": running, "address": f"http://{HOST}:{self.port}/" if running else None,
                    "port": self.port if running else None, "requests": self.requests,
                    "idle_stop_seconds": self.idle_stop_seconds, "stop_reason": self.stop_reason}

    # 函数用途: 记录一次请求活动（含被拒绝的请求），刷新空闲计时。
    def touch(self) -> None:
        with self.lock:
            self.requests += 1
            self.last_active = time.monotonic()

    # LLM: 常量时间比较；allow_query=False 时只认 cookie（API 路由）。返回 (是否通过, 令牌是否来自查询参数)。
    # 函数用途: 判断请求是否带了正确的会话令牌。
    def authorized(self, query_token: str | None, cookie_header: str | None, allow_query: bool) -> tuple[bool, bool]:
        if allow_query and query_token is not None and hmac.compare_digest(query_token.encode(), self.token.encode()):
            return True, True
        try:
            morsel = SimpleCookie(cookie_header or "").get(self.cookie_name)
        except CookieError:
            morsel = None
        return morsel is not None and hmac.compare_digest(morsel.value.encode(), self.token.encode()), False

    # LLM: 看守线程只在空闲超时时调用 close；close 之后线程退出。
    # 函数用途: 周期检查空闲时长，超时自动停止服务。
    def watch_idle(self) -> None:
        interval = min(1.0, self.idle_stop_seconds / 2)
        while not self.stopped.wait(interval):
            with self.lock:
                idle = time.monotonic() - self.last_active
            if idle >= self.idle_stop_seconds:
                self.close("idle")

    # LLM: 幂等；持锁完成 shutdown + server_close，返回时监听端口已关闭。不能在服务线程内调用（shutdown 会自等）。
    # 函数用途: 停止服务线程并释放端口，记录停止原因。
    def close(self, reason: str) -> None:
        with self.lock:
            if self.stop_reason is not None:
                return
            self.stop_reason = reason
            self.stopped.set()
            self.httpd.shutdown()
            self.httpd.server_close()


# LLM: 每个请求在独立线程中处理，只读 self.server.console；GET 以外的方法一律 405 且不读请求体。
# 类用途: 把 HTTP 请求分派到工作台页面和状态 API。
class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "harness-console"
    sys_version = ""

    # LLM: 先令牌后路由；/ 接受查询令牌或 cookie，/api/state 只认 cookie。
    # 函数用途: 处理 GET 请求。
    def do_GET(self) -> None:
        console = self.server.console
        console.touch()
        url = urlsplit(self.path)
        query = parse_qs(url.query, keep_blank_values=True)
        is_api = url.path == "/api/state"
        ok, from_query = console.authorized(query.get("token", [None])[0], self.headers.get("Cookie"), not is_api)
        if not ok:
            self.send_body(403, "text/plain; charset=utf-8", "缺少或错误的访问令牌。".encode())
            return
        if is_api:
            thread = query.get("thread", [""])[0]
            state = console_state(console.host, thread if 0 < len(thread) <= 200 else None, console.poll_seconds)
            self.send_body(200, "application/json; charset=utf-8", json.dumps(state, ensure_ascii=False).encode())
        elif url.path == "/":
            cookie = f"{console.cookie_name}={console.token}; HttpOnly; SameSite=Strict; Path=/" if from_query else None
            self.send_body(200, "text/html; charset=utf-8", console_page().encode(), cookie)
        else:
            self.send_body(404, "text/plain; charset=utf-8", "页面不存在。".encode())

    # LLM: 写方法和 HEAD 一律 405，不读请求体、不执行任何动作。
    # 函数用途: 拒绝 GET 以外的请求方法。
    def reject_method(self) -> None:
        self.server.console.touch()
        self.send_body(405, "text/plain; charset=utf-8", "只接受 GET。".encode(), extra={"Allow": "GET"},
                       head=self.command == "HEAD")

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = reject_method

    # 函数用途: 关闭默认访问日志，避免带令牌的 URL 写入 stderr。
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 基类签名
        return

    # LLM: 所有响应共用安全头：页面 CSP 只允许 self 与内联，禁止缓存和 Referer 外泄（令牌可能出现在 URL）。
    # 函数用途: 发送一个完整响应，可选附带首次访问时下发的 cookie。
    def send_body(self, status: int, content_type: str, body: bytes, cookie: str | None = None,
                  extra: dict | None = None, head: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", PAGE_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        headers = dict(extra or {})
        if cookie:
            headers["Set-Cookie"] = cookie
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)
