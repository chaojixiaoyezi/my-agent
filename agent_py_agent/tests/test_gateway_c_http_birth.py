"""原HTTP池Thread已出生但ACK异常时，空线程表不能冒充物理结清。"""
from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest

from agent_py_agent.agent.gateway_parts.bounded_http_server import GatewayBoundedHTTPServer
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_for_root
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.tests.test_gateway_background_curator_transactions import curator_case


class BirthAckFailure(BaseException):
    """可控的Python启动ACK故障，不发信号、不创建进程。"""


def test_original_pool_born_before_ack_failure_never_observes_settled(tmp_path, monkeypatch):
    born, release = threading.Event(), threading.Event()
    original_start, original_run = threading.Thread.start, threading.Thread.run
    threads = []
    fault = BirthAckFailure('synthetic original worker ACK failure')

    # LLM: 只延迟本次原pool的实际Thread进入initializer，真实start仍执行；精确原句柄最终必释放join，不删任何原表。
    # 函数用途: 重现原Thread已出生、原executor还未收到start ACK的窗口。
    def held_run(thread):
        if thread.name.startswith('gateway-http'):
            born.set()
            assert release.wait(5)
        return original_run(thread)

    def lost_ack(thread):
        original_start(thread)
        if thread.name.startswith('gateway-http'):
            threads.append(thread)
            assert born.wait(3)
            raise fault

    monkeypatch.setattr(threading.Thread, 'run', held_run)
    monkeypatch.setattr(threading.Thread, 'start', lost_ack)
    with curator_case(tmp_path, monkeypatch) as (_agent, _service, _backend, gate, _claim, _close):
        control = coordinator_for_root(gate.paths.root, context=gate.paths).control_plane
        runtime = GatewayDrainRuntime(control)
        group = runtime.prepare_pool('control', 'http/requests')
        server = GatewayBoundedHTTPServer(
            ('127.0.0.1', 0), BaseHTTPRequestHandler, bind_and_activate=False,
            request_worker_initializer=group.remember_current_thread,
            request_worker_failure=group.retain_fault,
            drain_runtime=runtime, drain_resource=group,
        )
        group.bind_executor(server._request_executor)
        request, peer = socket.socketpair()
        try:
            with pytest.raises(BirthAckFailure) as caught:
                server.process_request(request, ('127.0.0.1', 0))
            assert caught.value is fault
            assert len(threads) == 1 and threads[0].is_alive()
            traceback = caught.value.__traceback__
            original_in_trace = False
            while traceback is not None:
                original_in_trace |= traceback.tb_frame.f_locals.get('thread') is threads[0]
                traceback = traceback.tb_next
            assert original_in_trace
            assert not server._request_executor._threads and not group._born_threads
            group.stop()
            assert group.observe(time.monotonic() + .05) == 'unknown'
        finally:
            server.server_close()
            request.close()
            peer.close()
            release.set()
            for thread in threads:
                thread.join(3)
                assert not thread.is_alive()


def test_closed_original_c_cannot_create_first_lazy_http_pool_worker(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch) as (_agent, _service, _backend, gate, _claim, close):
        control = coordinator_for_root(gate.paths.root, context=gate.paths).control_plane
        runtime = GatewayDrainRuntime(control)
        group = runtime.prepare_pool('control', 'http/requests')
        server = GatewayBoundedHTTPServer(
            ('127.0.0.1', 0), BaseHTTPRequestHandler, bind_and_activate=False,
            request_worker_initializer=group.remember_current_thread,
            request_worker_failure=group.retain_fault,
            drain_runtime=runtime, drain_resource=group,
        )
        group.bind_executor(server._request_executor)
        request, peer = socket.socketpair()
        monkeypatch.setattr(server, 'finish_request', lambda *_: None)
        assert not server._request_executor._threads
        close()
        try:
            try:
                server.process_request(request, ('127.0.0.1', 0))
            except (RuntimeError, ValueError):
                pass
            assert not server._request_executor._threads
        finally:
            server.server_close()
            request.close()
            peer.close()
            for thread in tuple(server._request_executor._threads):
                thread.join(3)
                assert not thread.is_alive()
