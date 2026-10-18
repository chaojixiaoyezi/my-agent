# LLM: This module is the only transport-level concurrency implementation for
# Gateway HTTP requests. 原C设施先于pool/socket核源，finish_request区间才有原work且退出失效；原资源观察不授业务权；Keep it globally bounded and independent from
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
import time
from collections.abc import Callable
from concurrent.futures import Future
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from ..concurrency import DurableDaemonThreadPoolExecutor
from .multiprocess_http_transport import (
    http_request_work_scope,
    original_http_birth,
    require_http_worker_source,
)


# LLM: This is the sole HTTP concurrency owner for the single Gateway. Keep the
# accept backlog, reusable worker pool, and outstanding-request admission bound
# together; never infer owner identity from an unauthenticated socket/header.
# Handlers retain all authorization and durable per-owner operation semantics; 原work仅绑定本设施寿命，不扩大任何owner业务权限。
# 类用途: 用固定工作线程和有界排队承接多个 TUI，避免每次轮询都新建系统线程。
class GatewayBoundedHTTPServer(HTTPServer):
    request_queue_size = 128
    max_request_workers = 16
    max_outstanding_requests = 128
    request_socket_timeout = 5.0
    request_queue_timeout = 2.0

    # LLM: Construction must establish admission before serve_forever can
    # accept work. The daemon executor preserves the previous restart behavior:
    # an abandoned client must not keep the Gateway process alive.
    # LLM: 原C runtime/group与观察者在pool/socket前核，保存同一executor/runtime原引用供work末核；Count0不建work且不增加IO，W/host拒绝。
    # 函数用途: 先检查同次设施来源，再初始化原有界监听器和工作池。
    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        bind_and_activate: bool = True,
        *,
        request_worker_initializer: Callable[[], None] | None = None,
        request_worker_failure: Callable[[BaseException], None] | None = None,
        drain_runtime: Any = None,
        drain_resource: Any = None,
    ) -> None:
        require_http_worker_source(drain_runtime, drain_resource, request_worker_initializer, request_worker_failure)
        workers = max(1, int(self.max_request_workers))
        capacity = max(workers, int(self.max_outstanding_requests))
        self._request_slots = threading.BoundedSemaphore(capacity)
        with original_http_birth(drain_runtime):
            self._request_executor = DurableDaemonThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="gateway-http",
                initializer=request_worker_initializer,
            )
            if drain_runtime is not None:
                self._request_executor_origin = self._request_executor
            self._request_worker_failure = request_worker_failure
            self._request_drain_runtime = self._request_drain_runtime_origin = drain_runtime
            self._closing = threading.Event()
            super().__init__(
                server_address,
                request_handler_class,
                bind_and_activate=bind_and_activate,
            )

    # LLM: 在途数包含运行和排队；从接收时计排队时限，socket 超时只约束传输，不中止已接收的持久任务。
    # LLM: 懒submit可能创建Thread，须同原runtime的controller→lease→runtime末门；等待/handler不在门内。任意submit异常保原group未知出生，普通忙错误沿原503，其余同对象上抛。
    # 函数用途: 接收有界连接，提交失败不丢原Thread责任或把中断误当繁忙重试。
    def process_request(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        if self._closing.is_set() or not self._request_slots.acquire(blocking=False):
            self._reject_overloaded_request(request)
            return
        try:
            request.settimeout(self.request_socket_timeout)
            if self._request_drain_runtime is not self._request_drain_runtime_origin:
                raise ValueError("http_original_runtime_required")
            with original_http_birth(self._request_drain_runtime):
                future = self._request_executor.submit(
                    self._process_request_worker,
                    request,
                    client_address,
                    time.monotonic(),
                )
        except (RuntimeError, OSError) as error:
            if self._request_worker_failure is not None:
                self._request_worker_failure(error)
            self._request_slots.release()
            self._reject_overloaded_request(request)
            return
        except BaseException as error:
            try:
                if self._request_worker_failure is not None:
                    self._request_worker_failure(error)
            finally:
                self._request_slots.release()
                try:
                    self.shutdown_request(request)
                finally:
                    raise error
        future.add_done_callback(
            lambda completed, selected=request: self._request_finished(
                completed,
                selected,
            )
        )

    # LLM: 排队过期不进入handler；C只在原finish_request区间安装原pool工作寿命，异常/退出即失效，不持Root门跨handler，Count0原分支不建来源。
    # 函数用途: 处理原连接并限定本次C元数据来源寿命；保原异常策略与所有分支关闭socket。
    def _process_request_worker(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        accepted_at: float,
    ) -> None:
        if time.monotonic() - accepted_at > self.request_queue_timeout:
            self._reject_overloaded_request(request)
            return
        try:
            if self._request_drain_runtime is None and self._request_drain_runtime_origin is None:
                self.finish_request(request, client_address)
            else:
                with http_request_work_scope(self, request):
                    self.finish_request(request, client_address)
        except (TimeoutError, ConnectionError):
            pass
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
