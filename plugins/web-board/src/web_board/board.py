# LLM: 本模块在插件进程内起标准库 ThreadingHTTPServer：只绑定 127.0.0.1 随机端口，只接受 GET/HEAD，每个请求
#   先校验令牌（查询参数或 HttpOnly cookie），再用 serve 时冻结的读取上下文 check + SDK no-follow 读取；绝不写文件。
#   副作用：占用一个本地端口、启动服务线程和空闲看守线程（均为 daemon，进程退出时随之结束）。
#   不写 stderr 访问日志，避免把带令牌的 URL 泄露到宿主日志。
# 模块用途: 实现一次网页浏览服务的完整生命周期（启动、请求处理、计数、空闲自停、关闭释放端口）。

from __future__ import annotations

import hmac
import mimetypes
import os
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from my_agent_plugin_api.nofollow_fs import NoFollowPathError
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext

from .pages import error_page, listing_page, preview_kind, view_page
from .reading import BoardError, list_directory, open_file, read_head, request_target

HOST = "127.0.0.1"
PAGE_POLICY = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; frame-src 'self'; "
               "base-uri 'none'; form-action 'none'")


# LLM: 服务状态全部在实例内，由 lock 保护；close 幂等且持锁完成，返回时端口已释放。上下文在构造时冻结，
#   之后不接受新的权限；再次 serve 必须新建实例。
# 类用途: 表示一次 serve 调用启动的网页服务。
class BoardService:
    # LLM: 构造即绑定端口并启动两个 daemon 线程；root 必须已由 authorized_root 验证。
    # 函数用途: 生成访问令牌、绑定随机回环端口并开始提供服务。
    def __init__(self, context: WorkspaceReadContext, root: Path, *, max_preview_bytes: int, idle_stop_seconds: int):
        self.context, self.root = context, root
        self.display = str(root.relative_to(context.cwd)) if root.is_relative_to(context.cwd) else str(root)
        self.max_preview_bytes, self.idle_stop_seconds = max_preview_bytes, idle_stop_seconds
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.requests, self.last_active = 0, time.monotonic()
        self.stop_reason: str | None = None
        self.stopped = threading.Event()
        self.httpd = ThreadingHTTPServer((HOST, 0), BoardHandler)
        self.httpd.board = self
        self.port = self.httpd.server_address[1]
        self.cookie_name = f"web_board_{self.port}"
        threading.Thread(target=self.httpd.serve_forever, name="web-board-http", daemon=True).start()
        threading.Thread(target=self.watch_idle, name="web-board-idle", daemon=True).start()

    # 函数用途: 返回带令牌的访问地址。
    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/?token={self.token}"

    # LLM: 只读快照；被 status 工具调用，不改变服务状态。
    # 函数用途: 汇报是否在服务、地址、目录和已处理请求数。
    def snapshot(self) -> dict:
        with self.lock:
            running = self.stop_reason is None
            # 结果会进入模型上下文与历史，只给不含令牌的地址；完整链接由 server 写入私有文件并直接在浏览器打开
            return {"serving": running, "address": f"http://{HOST}:{self.port}/" if running else None, "root": self.display,
                    "port": self.port if running else None, "requests": self.requests,
                    "idle_stop_seconds": self.idle_stop_seconds, "stop_reason": self.stop_reason}

    # LLM: 每个请求（含被拒绝的）都计数并刷新空闲计时。
    # 函数用途: 记录一次请求活动。
    def touch(self) -> None:
        with self.lock:
            self.requests += 1
            self.last_active = time.monotonic()

    # LLM: 常量时间比较；query 与 cookie 任一正确即通过。
    # 函数用途: 判断请求是否带了正确令牌，并返回令牌是否来自查询参数（用于决定是否下发 cookie）。
    def authorized(self, query_token: str | None, cookie_header: str | None) -> tuple[bool, bool]:
        if query_token is not None and hmac.compare_digest(query_token.encode(), self.token.encode()):
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


# LLM: 每个请求在独立线程中处理；只读取 self.server.board 上冻结的事实。未定义的方法由基类返回 501，
#   常见写方法显式返回 405。log_message 被静音以免令牌进入日志。
# 类用途: 把 HTTP 请求分派到目录列表、预览、原始文件三个只读页面。
class BoardHandler(BaseHTTPRequestHandler):
    server_version = "web-board"
    sys_version = ""

    # 函数用途: 处理 GET 请求。
    def do_GET(self) -> None:
        self.respond(head=False)

    # 函数用途: 处理 HEAD 请求（只发头部）。
    def do_HEAD(self) -> None:
        self.respond(head=True)

    # LLM: 写方法一律 405，不读请求体、不执行任何动作。
    # 函数用途: 拒绝 POST/PUT/DELETE/PATCH/OPTIONS。
    def reject_method(self) -> None:
        self.server.board.touch()
        self.send_text(405, "只接受 GET/HEAD。", head=False, extra={"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = reject_method

    # 函数用途: 关闭默认访问日志，避免带令牌的 URL 写入 stderr。
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 基类签名
        return

    # LLM: 先令牌后路由；路由内所有路径都经 request_target（冻结上下文 check）和 no-follow 打开。
    #   链接/非普通对象 -> 403，缺失 -> 404，其它读取失败 -> 404，不回显原始异常。
    # 函数用途: 处理一个只读请求并写回响应。
    def respond(self, head: bool) -> None:
        board = self.server.board
        board.touch()
        url = urlsplit(self.path)
        query = parse_qs(url.query, keep_blank_values=True)
        ok, from_query = board.authorized(query.get("token", [None])[0], self.headers.get("Cookie"))
        if not ok:
            self.send_text(403, "缺少或错误的访问令牌。", head=head)
            return
        cookie = (f"{board.cookie_name}={board.token}; HttpOnly; SameSite=Strict; Path=/"
                  if from_query else None)
        try:
            target, relative = request_target(board.context, board.root, query.get("p", [""])[0])
            if url.path == "/":
                entries, truncated = list_directory(board.context, target)
                self.send_page(200, listing_page(board.display, relative, entries, truncated), head, cookie)
            elif url.path == "/view":
                kind = preview_kind(target.name)
                # 图片只取大小，内容由 <img> 走 /raw 另行读取；其它类型按上限读开头
                content, size, truncated = read_head(target, 0 if kind == "image" else board.max_preview_bytes)
                self.send_page(200, view_page(board.display, relative, kind, content, size, truncated), head, cookie)
            elif url.path == "/raw":
                self.send_raw(target, head)
            else:
                raise BoardError(404, "NOT_FOUND", "页面不存在。")
        except BoardError as exc:
            self.send_page(exc.status, error_page(board.display, exc.status, str(exc)), head, cookie)
        except NoFollowPathError:
            self.send_page(403, error_page(board.display, 403, "路径含链接、多链接文件或非普通对象，已拒绝。"), head, cookie)
        except (FileNotFoundError, NotADirectoryError):
            self.send_page(404, error_page(board.display, 404, "对象不存在或类型不符。"), head, cookie)
        except OSError:
            self.send_page(404, error_page(board.display, 404, "无法读取该对象。"), head, cookie)

    # LLM: 原始字节以推测的类型发出，同时加 CSP sandbox 和 nosniff，直接打开 .html/.svg 也不会执行脚本。
    # 函数用途: 流式发送一个普通文件的原始内容。
    def send_raw(self, target: Path, head: bool) -> None:
        descriptor = open_file(target)
        try:
            size = os.fstat(descriptor).st_size
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
            self.common_headers()
            self.end_headers()
            remaining = 0 if head else size
            while remaining > 0 and (chunk := os.read(descriptor, min(65536, remaining))):
                self.wfile.write(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)

    # 函数用途: 发送一个 HTML 页面，可选附带首次访问时下发的令牌 cookie。
    def send_page(self, status: int, page: str, head: bool, cookie: str | None) -> None:
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", PAGE_POLICY)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.common_headers()
        self.end_headers()
        if not head:
            self.wfile.write(body)

    # 函数用途: 发送纯文本错误响应（令牌错误、方法不允许）。
    def send_text(self, status: int, message: str, head: bool, extra: dict | None = None) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.common_headers()
        self.end_headers()
        if not head:
            self.wfile.write(body)

    # LLM: 令牌可能出现在 URL，所以禁止 Referer 外泄和缓存。
    # 函数用途: 写入所有响应共用的安全头。
    def common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
