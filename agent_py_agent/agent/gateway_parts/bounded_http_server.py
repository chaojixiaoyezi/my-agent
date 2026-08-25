# LLM: This module is the only transport-level concurrency implementation for
# Gateway HTTP requests. Keep it globally bounded and independent from
# authenticated owner/task/run policy; handlers own identity-scoped admission.
# 模块用途: 为单 Gateway 提供可复用、有容量上限的 HTTP 工作线程，避免多 TUI 轮询拖垮进程。
"""Bounded, reusable HTTP request workers for the single local Gateway.

The stdlib ``ThreadingHTTPServer`` creates one operating-system thread per
request.  Several long-lived TUIs poll the same Gateway, so that design keeps
creating short-lived threads and repeatedly allocates large activity snapshots.
This module keeps the stdlib HTTP surface while replacing thread-per-request
with a fixed daemon pool and a hard outstanding-request bound.
"""

from __future__ import annotations

import json
import socket
import threading
from concurrent.futures import Future
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from ..concurrency import DurableDaemonThreadPoolExecutor


# LLM: This is the sole HTTP concurrency owner for the single Gateway. Keep the
# accept backlog, reusable worker pool, and outstanding-request admission bound
# together; never infer owner identity from an unauthenticated socket/header.
# Handlers retain all authorization and durable per-owner operation semantics.
# 类用途: 用固定工作线程和有界排队承接多个 TUI，避免每次轮询都新建系统线程。
class GatewayBoundedHTTPServer(HTTPServer):
    request_queue_size = 128
    max_request_workers = 16
    max_outstanding_requests = 128

    # LLM: Construction must establish admission before serve_forever can
    # accept work. The daemon executor preserves the previous restart behavior:
    # an abandoned client must not keep the Gateway process alive.
    # 函数用途: 初始化监听器、固定 HTTP 工作池和全局在途请求槽位。
    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        bind_and_activate: bool = True,
    ) -> None:
        workers = max(1, int(self.max_request_workers))
        capacity = max(workers, int(self.max_outstanding_requests))
        self._request_slots = threading.BoundedSemaphore(capacity)
        self._request_executor = DurableDaemonThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="gateway-http",
        )
        self._closing = threading.Event()
        super().__init__(
            server_address,
            request_handler_class,
            bind_and_activate=bind_and_activate,
        )

    # LLM: Admission counts both running and queued requests. Never block the
    # accept loop waiting for a slot: overload must be explicit so TUI transport
    # backoff can engage instead of opening more sockets.
    # 函数用途: 将请求放入固定工作池；容量已满时立即返回 503，防止内存和线程无界增长。
    def process_request(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        if self._closing.is_set() or not self._request_slots.acquire(blocking=False):
            self._reject_overloaded_request(request)
            return
        try:
            future = self._request_executor.submit(
                self._process_request_worker,
                request,
                client_address,
            )
        except RuntimeError:
            self._request_slots.release()
            self._reject_overloaded_request(request)
            return
        future.add_done_callback(
            lambda completed, selected=request: self._request_finished(
                completed,
                selected,
            )
        )

    # LLM: This mirrors socketserver.ThreadingMixIn.process_request_thread:
    # handler exceptions are isolated and every accepted socket is closed once.
    # 函数用途: 在复用的 worker 中执行一个 HTTP handler，并在结束后关闭连接。
    def _process_request_worker(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    # LLM: A queued request cancelled during shutdown never reaches the worker,
    # so its socket must be closed here. Slot release happens exactly once for
    # every successful submit, regardless of completion outcome.
    # 函数用途: 回收请求槽位，并关闭停机时尚未开始处理的连接。
    def _request_finished(
        self,
        future: Future[Any],
        request: socket.socket,
    ) -> None:
        if future.cancelled():
            self.shutdown_request(request)
        self._request_slots.release()

    # LLM: Overload is transport state, not task failure. Return a typed,
    # credential-free 503 with Retry-After and never enter a product handler.
    # 函数用途: 在 HTTP 层明确告知客户端网关繁忙，让现有退避逻辑稍后重连。
    def _reject_overloaded_request(self, request: socket.socket) -> None:
        payload = json.dumps(
            {
                "ok": False,
                "error_code": "GATEWAY_HTTP_BUSY",
                "message": "Gateway 当前连接繁忙，请稍后重试。",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        response = (
            f"HTTP/1.1 {HTTPStatus.SERVICE_UNAVAILABLE.value} "
            f"{HTTPStatus.SERVICE_UNAVAILABLE.phrase}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Retry-After: 1\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii") + payload
        try:
            request.settimeout(0.2)
            request.sendall(response)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

    # LLM: Stop admission before cancelling queued work. Running daemon workers
    # may finish their current durable handler, matching the former daemon-thread
    # shutdown boundary without an interpreter-wide join.
    # 函数用途: 停止接收新请求，取消尚未执行的排队请求并关闭监听套接字。
    def server_close(self) -> None:
        self._closing.set()
        self._request_executor.shutdown(wait=False, cancel_futures=True)
        super().server_close()


__all__ = ["GatewayBoundedHTTPServer"]
