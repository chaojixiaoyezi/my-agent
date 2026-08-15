#!/usr/bin/env python3
"""R1-02 注入代理: CONNECT 隧道转发, 可选在流中途断开客户端连接。

模式(mode):
  relay       全转发 —— 语义等价对照(先对照后注入铁律)
  rst         注入一次: 转发 --inject-bytes 字节后 RST 客户端连接
              -> 客户端 ConnectionResetError/SSLEOFError(OSError 子类)
              -> _runtime_network_error 文本命中 connection reset/eof
              -> typed ProviderTransientError -> 同回合自动阶梯重试
  truncate    注入一次: 转发 --inject-bytes 字节后优雅 FIN(半截 body)
              -> 客户端 IncompleteRead(http.client.HTTPException,
                 不在 _post_stream_lines 捕获元组) -> 裸冒泡
              -> attempt 失败 -> 唤醒轮续跑

只注入目标 host(--inject-host)的 CONNECT 隧道一次; 注入时机 = 转发字节数
达阈值且连接已存活 --inject-after-seconds(保证流已开始, 不在 TLS 握手期)。

用法:
  python3 scripts/run_r1_02_proxy.py --listen 127.0.0.1:18421 \
      --mode rst --inject-host api.minimaxi.com \
      --inject-bytes 8192 --inject-after-seconds 3 \
      --log /tmp/r1-02-proxy.log
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

_IDLE_EXIT_SECONDS = 300.0  # 隧道空闲(双向无数据)超过该时长则保守退出防挂死


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:18421")
    parser.add_argument("--mode", choices=("relay", "rst", "truncate"), default="relay")
    parser.add_argument("--inject-host", default="")
    parser.add_argument("--inject-bytes", type=int, default=8192,
                        help="隧道内转发字节阈值(TLS 密文计数, 8KB ≈ 已过握手+首帧)")
    parser.add_argument("--inject-after-seconds", type=float, default=3.0,
                        help="连接存活秒数阈值, 双条件都要满足才注入")
    parser.add_argument("--inject-once", action="store_true",
                        help="全局只注入一次: 首个满足条件的隧道注入后, 后续所有"
                             "隧道不再注入(语义 = 单次断流注入, 观察客户端恢复)")
    parser.add_argument("--log", default="/tmp/r1-02-proxy.log")
    parser.add_argument("--upstream-addr", default="",
                        help="固定上游 host:port(缺省直连 CONNECT 目标)")
    return parser.parse_args()


class _Log:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._path = path

    def write(self, event: dict) -> None:
        line = json.dumps({"ts": time.strftime("%Y%m%dT%H%M%S%fZ", time.gmtime()),
                           **event}, ensure_ascii=False)
        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
        print(line, flush=True)


def _relay_tunnel(client: socket.socket, upstream: socket.socket,
                  log: _Log, args: argparse.Namespace, conn_id: str,
                  target: str, initial: bytes = b"",
                  server: "_Server | None" = None) -> None:
    """双向转发; 上游->客户端方向计数, 达标后按 mode 断客户端连接。

    实现为每方向一个独立泵线程 + 阻塞 sendall。旧单线程 select 实现在
    非阻塞 socket 上 sendall 大数据块遇发送缓冲满时抛 BlockingIOError
    (OSError 子类), 被误当对端关闭 -> 大请求/大响应半路断连
    (RemoteDisconnected)。阻塞 sendall 由内核排队, 永不丢数据、永不误关。

    initial = CONNECT 请求之后 rfile 缓冲里已有的隧道首包(TLS ClientHello
    常与 CONNECT 请求同 TCP 段, 被 readline 缓冲走), 先转给上游。
    """
    client.settimeout(1.0)
    upstream.settimeout(1.0)
    state: dict = {
        "forwarded": 0,        # 上游->客户端累计字节(注入判据)
        "injected": False,
        "opened_at": time.monotonic(),
        "last_activity": time.monotonic(),
        "stop": threading.Event(),
    }

    def _inject_now(force: bool) -> bool:
        """当前隧道是否应注入; --inject-once 下全局已注入则不再注入。"""
        if state["injected"]:
            return False
        if args.inject_once and server is not None and server.inject_used:
            return False
        if not _should_inject(args, target, state["forwarded"],
                              time.monotonic() - state["opened_at"], force=force):
            return False
        _do_inject(client, upstream, log, args, conn_id, target,
                   state["forwarded"], time.monotonic() - state["opened_at"])
        state["injected"] = True
        if args.inject_once and server is not None:
            server.mark_injected()
        return True

    def _pump(src: socket.socket, dst: socket.socket, toward_client: bool) -> None:
        while not state["stop"].is_set():
            try:
                data = src.recv(65536)
            except socket.timeout:
                # 1s 读超时仅用于空闲检测; 双向都空闲超 _IDLE_EXIT_SECONDS 则退出
                if time.monotonic() - state["last_activity"] > _IDLE_EXIT_SECONDS:
                    log.write({"type": "idle_exit", "conn": conn_id, "target": target})
                    state["stop"].set()
                continue
            except OSError:
                return
            if not data:
                # EOF 前最后一次注入机会: 单批小响应场景下中间检查点
                # 可能从未满足(字节/时间条件), 上游已结束 = 不再有数据
                if toward_client and _inject_now(force=True):
                    state["stop"].set()
                    return
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                return
            state["last_activity"] = time.monotonic()
            if toward_client:
                state["forwarded"] += len(data)
                if _inject_now(force=False):
                    # 注入: 这批数据已完整送达, 再按模式断客户端连接
                    state["stop"].set()
                    return
            try:
                dst.sendall(data)
            except OSError:
                return

    if initial:
        try:
            upstream.sendall(initial)
        except OSError:
            return
    pumps = [
        threading.Thread(target=_pump, args=(client, upstream, False), daemon=True),
        threading.Thread(target=_pump, args=(upstream, client, True), daemon=True),
    ]
    for t in pumps:
        t.start()
    for t in pumps:
        # 阻塞 sendall 卡死场景(对端不读)由 join 超时兜底, close 打断后线程退出
        t.join(timeout=5)
    for s in (client, upstream):
        try:
            s.close()
        except OSError:
            pass


def _do_inject(client: socket.socket, upstream: socket.socket,
               log: _Log, args: argparse.Namespace, conn_id: str,
               target: str, forwarded: int, elapsed: float) -> None:
    log.write({"type": "inject", "conn": conn_id, "target": target,
               "mode": args.mode, "forwarded_bytes": forwarded,
               "after_seconds": round(elapsed, 2)})
    try:
        if args.mode == "rst":
            # 有未读/未写数据的 socket shutdown(SHUT_RDWR) 产生 RST
            client.shutdown(socket.SHUT_RDWR)
        else:
            # truncate: 停发送方向发 FIN —— 客户端读到半截 body 后 EOF
            # (SO_LINGER=0 是立即 RST 非 FIN, 那会混入 rst 路径)
            client.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    try:
        client.close()
    except OSError:
        pass
    # 上游侧继续读完, 防上游写阻塞
    _drain(upstream)


def _should_inject(args: argparse.Namespace, target: str, forwarded: int,
                   elapsed: float, force: bool = False) -> bool:
    if args.mode == "relay":
        return False
    if args.inject_host and args.inject_host not in target:
        return False
    if forwarded < args.inject_bytes:
        return False
    if not force and elapsed < args.inject_after_seconds:
        return False
    return True


def _drain(sock: socket.socket) -> None:
    deadline = time.monotonic() + 3.0
    sock.setblocking(False)
    while time.monotonic() < deadline:
        try:
            data = sock.recv(65536)
            if not data:
                return
        except OSError:
            return
        except socket.timeout:
            return


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "r1-02-proxy/1.0"

    def log_message(self, *_: object) -> None:
        pass

    def do_CONNECT(self) -> None:  # noqa: N802
        log: _Log = self.server.log  # type: ignore[attr-defined]
        args: argparse.Namespace = self.server.args  # type: ignore[attr-defined]
        conn_id = f"{int(time.time() * 1000)}-{self.connection.getpeername()[1]}"
        target = self.path
        try:
            host, port = _resolve_upstream(target, args)
            upstream = socket.create_connection((host, port), timeout=10.0)
        except OSError as exc:
            log.write({"type": "connect_failed", "conn": conn_id, "target": target,
                       "error": f"{type(exc).__name__}: {exc}"})
            self.send_error(502)
            return
        log.write({"type": "connect", "conn": conn_id, "target": target,
                   "mode": args.mode})
        # 必须先回 200: HTTP 协议下客户端收到 200 才发 TLS ClientHello
        self.send_response(200, "Connection established")
        self.end_headers()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # 200 之后 ClientHello 到达, 可能被 rfile 缓冲; 非阻塞取一次
        # (缓冲为空 = 客户端还没发, 交给转发循环 select 处理)
        pending = b""
        try:
            self.connection.setblocking(False)
            pending = self.rfile.read1()
        except (BlockingIOError, OSError, ValueError):
            pending = b""
        # 本线程内阻塞转发: handler 不返回, 避免 finish()/下一轮读请求竞争 socket
        self.close_connection = True
        _relay_tunnel(self.connection, upstream, log, args, conn_id, target,
                      initial=pending, server=self.server)  # type: ignore[arg-type]

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


def _resolve_upstream(target: str, args: argparse.Namespace) -> tuple[str, int]:
    if args.upstream_addr:
        host, port = args.upstream_addr.rsplit(":", 1)
        return host, int(port)
    parts = urlsplit("//" + target)
    host = parts.hostname or target
    port = parts.port or 443
    return host, int(port)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], handler, log: _Log,
                 args: argparse.Namespace) -> None:
        super().__init__(addr, handler)
        self.log = log  # type: ignore[attr-defined]
        self.args = args  # type: ignore[attr-defined]
        self.inject_used = False
        self._inject_lock = threading.Lock()

    def mark_injected(self) -> None:
        with self._inject_lock:
            self.inject_used = True


def _main() -> int:
    args = _parse_args()
    host, port = args.listen.rsplit(":", 1)
    log = _Log(args.log)
    log.write({"type": "start", "mode": args.mode,
               "inject_host": args.inject_host, "listen": args.listen,
               "inject_bytes": args.inject_bytes,
               "inject_after_seconds": args.inject_after_seconds})
    server = _Server((host, int(port)), _Handler, log, args)
    log.write({"type": "listening", "listen": args.listen})
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.write({"type": "stopped"})
    return 0


if __name__ == "__main__":
    sys.exit(_main())
