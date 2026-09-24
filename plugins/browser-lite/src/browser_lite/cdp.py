# LLM: 一条直连页面目标的 CDP 会话：命令按 id 等响应，事件进有界队列；Fetch.requestPaused 按当前地址守卫即时放行或拦截，
#   页面崩溃事件立即转成 PAGE_CRASHED。每个命令都有超时（设置 command_timeout_seconds），超时不重发。
# 模块用途: 在最小 WebSocket 客户端之上实现 CDP 命令/事件收发与请求拦截。

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable
from urllib.parse import urlsplit

from .errors import BrowserError
from .websocket import WebSocketClient, WebSocketError

# 请求拦截只裁决这些协议；data:/blob: 等不出网的内嵌资源直接放行
_GUARDED_SCHEMES = {"http", "https", "file"}


# LLM: 一个实例对应一个页面目标；guard 是本次工具调用绑定的地址裁决函数（返回拒绝原因或 None），
#   调用之间收到的暂停请求会留在浏览器里，等下一次调用读消息时用新的 guard 裁决。
# 类用途: 页面级 CDP 连接。
class PageConnection:
    # LLM: 只保存连接与超时；事件队列有上限，丢弃最旧事件而不是无界增长。
    # 函数用途: 绑定已握手的 WebSocket 和命令超时。
    def __init__(self, ws: WebSocketClient, timeout: float):
        self.ws = ws
        self.timeout = timeout
        self.guard: Callable[[str], str | None] | None = None
        self.blocked: list[str] = []
        self.events: deque[dict] = deque(maxlen=1000)
        self.main_frame = ""
        self._next_id = 1

    # LLM: 有副作用：建立 WebSocket、开启 Page/Inspector 事件和全量请求拦截，并记录主 frame id。失败时关闭连接。
    # 函数用途: 连接页面目标并完成本插件需要的 CDP 初始化。
    @classmethod
    def open(cls, port: int, path: str, timeout: float) -> PageConnection:
        try:
            ws = WebSocketClient.connect("127.0.0.1", port, path, timeout)
        except (OSError, WebSocketError) as exc:
            raise BrowserError("BROWSER_DISCONNECTED", "无法连接浏览器调试端口。") from exc
        page = cls(ws, timeout)
        try:
            page.call("Page.enable")
            page.call("Inspector.enable")
            page.call("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
            page.main_frame = page.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        except BaseException:
            page.close()
            raise
        return page

    # LLM: 等待期间照常处理事件（含请求拦截）；其它 id 的响应直接丢弃（只可能是 notify 的回执）。
    #   CDP 返回 error 时抛 CDP_ERROR，超时抛 TIMEOUT，连接断开抛 BROWSER_DISCONNECTED。
    # 函数用途: 发送一个 CDP 命令并等待它的结果。
    def call(self, method: str, params: dict | None = None) -> dict:
        request_id = self.notify(method, params)
        deadline = time.monotonic() + self.timeout
        while True:
            message = self._receive(deadline)
            if message is None:
                raise BrowserError("TIMEOUT", f"浏览器命令超时（{self.timeout:g} 秒）：{method}")
            if message.get("id") == request_id:
                if "error" in message:
                    detail = str((message["error"] or {}).get("message", ""))[:200]
                    raise BrowserError("CDP_ERROR", f"浏览器命令 {method} 失败：{detail}")
                return message.get("result") or {}

    # LLM: 只发不等；用于拦截放行这类必须在事件处理中立即回复、又不能重入 call 的命令。
    # 函数用途: 发送一个 CDP 命令并返回它的 id。
    def notify(self, method: str, params: dict | None = None) -> int:
        request_id, self._next_id = self._next_id, self._next_id + 1
        try:
            self.ws.send_text(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        except (OSError, WebSocketError) as exc:
            raise BrowserError("BROWSER_DISCONNECTED", "浏览器连接已断开。") from exc
        return request_id

    # LLM: 先查已排队事件再读新消息；到 timeout 仍没等到返回 None（不是错误，调用方决定是否超时）。
    # 函数用途: 等待第一个满足条件的 CDP 事件。
    def wait_event(self, predicate: Callable[[dict], bool], timeout: float) -> dict | None:
        for event in list(self.events):
            if predicate(event):
                self.events.remove(event)
                return event
        deadline = time.monotonic() + timeout
        while True:
            message = self._receive(deadline)
            if message is None:
                return None
            if "method" in message and message in self.events and predicate(message):
                self.events.remove(message)
                return message

    # LLM: 返回下一条消息，截止返回 None；事件在这里统一分流：崩溃抛错、拦截即时裁决、其余入队。
    # 函数用途: 读取并预处理一条 CDP 消息。
    def _receive(self, deadline: float) -> dict | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            message = json.loads(self.ws.recv_text(remaining))
        except TimeoutError:
            return None
        except (OSError, ValueError, WebSocketError) as exc:
            raise BrowserError("BROWSER_DISCONNECTED", "浏览器连接已断开。") from exc
        method = message.get("method") if isinstance(message, dict) else None
        if method == "Inspector.targetCrashed":
            raise BrowserError("PAGE_CRASHED", "页面崩溃，浏览器已关闭；请重新 open。")
        if method == "Fetch.requestPaused":
            self._decide(message.get("params") or {})
        elif method:
            self.events.append(message)
        return message if isinstance(message, dict) else {}

    # LLM: 受控协议的请求由 guard 裁决，被拒的地址记入 blocked（供上层报告“跳到不允许地址”）并以 BlockedByClient 失败；
    #   没有 guard 时一律拦截，宁可失败也不放行。
    # 函数用途: 对一个被暂停的网络请求做放行或拦截。
    def _decide(self, params: dict) -> None:
        url = str((params.get("request") or {}).get("url", ""))
        scheme = urlsplit(url).scheme.lower()
        allowed = scheme not in _GUARDED_SCHEMES or (self.guard is not None and self.guard(url) is None)
        if allowed:
            self.notify("Fetch.continueRequest", {"requestId": params.get("requestId")})
        else:
            self.blocked.append(url[:500])
            self.notify("Fetch.failRequest", {"requestId": params.get("requestId"), "errorReason": "BlockedByClient"})

    # LLM: 幂等，只关连接，不结束浏览器进程。
    # 函数用途: 关闭页面 CDP 连接。
    def close(self) -> None:
        self.ws.close()
