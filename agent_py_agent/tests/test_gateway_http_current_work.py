"""实际原HTTP池工作区间的来源；仅替换socket平台边界，不造Agent、registry或执行权。"""
import copy
import threading
import time
from contextlib import contextmanager
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_http_transport as transport
from agent_py_agent.agent.gateway_parts.bounded_http_server import (
    GatewayBoundedHTTPServer,
    HTTPServer,
)
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context


# LLM: 原C/Root/group/lower server与真实executor均按产品构造；仅隔离父HTTPServer的socket初始化，工作仍运行原pool方法。
# 函数用途: 用原池线程和真实DrainResource夹具验证来源，不监听端口或执行客户端请求。
@contextmanager
def http_case(control, monkeypatch, *, count0=False):
    monkeypatch.setattr(HTTPServer, '__init__', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(GatewayBoundedHTTPServer, 'max_request_workers', 1)
    runtime = None if count0 else GatewayDrainRuntime(control)
    group = None if count0 else runtime.prepare_pool('control', 'http/requests')
    server = GatewayBoundedHTTPServer(('127.0.0.1', 0), object, bind_and_activate=False,
        drain_runtime=runtime, drain_resource=group,
        request_worker_initializer=group.remember_current_thread if group else None,
        request_worker_failure=group.retain_fault if group else None)
    if group:
        group.bind_executor(server._request_executor)
    request = object()
    handler = SimpleNamespace(server=server, request=request, connection=request)
    server.shutdown_request = lambda _request: None
    server.handle_error = lambda *_args: None
    try:
        yield server, runtime, group, request, handler
    finally:
        server._request_executor.shutdown(wait=True, cancel_futures=True)


# LLM: 提交的是真实原_process_request_worker，finish_request仅是受控平台替身；future本身不成为来源。
# 函数用途: 在实际登记的池worker内进入原HTTP工作区间并等待本测试完成。
def run_work(server, request, operation):
    server.finish_request = lambda *_args: operation()
    return server._request_executor.submit(server._process_request_worker,
        request, ('127.0.0.1', 1), time.monotonic()).result(timeout=3)


def test_actual_pool_finish_request_publishes_work_only_during_original_interval(context, monkeypatch):
    with http_case(context, monkeypatch) as (server, runtime, _group, request, _handler):
        observed = []

        def finish():
            query = getattr(transport, 'current_http_request_work', lambda: None)
            observed.append(query())

        run_work(server, request, finish)
        assert len(observed) == 1 and observed[0] is not None
        assert observed[0].server is server and observed[0].runtime is runtime
        assert observed[0].executor is server._request_executor
        assert transport.current_http_request_work() is None


def test_capture_and_last_require_are_same_original_work(context, monkeypatch):
    with http_case(context, monkeypatch) as (server, runtime, group, request, handler):
        seen = []

        def finish():
            work = transport.capture_http_request_work(handler)
            assert transport.require_http_request_work(work, handler=handler, server=server) is work
            assert work.thread is threading.current_thread()
            assert group._born_threads.get(work.thread) is work.thread
            assert work.control is context and work.runtime is runtime
            seen.append(work)

        run_work(server, request, finish)
        assert len(seen) == 1
        with pytest.raises(ValueError):
            transport.require_http_request_work(seen[0], handler=handler)


@pytest.mark.parametrize('fault', ['copy', 'other_handler', 'socket', 'server', 'executor', 'runtime', 'controller', 'closing'])
def test_actual_work_rejects_identity_or_root_drift(context, monkeypatch, fault):
    with http_case(context, monkeypatch) as (server, runtime, _group, request, handler):
        checked = []

        def finish():
            work = transport.capture_http_request_work(handler)
            selected = copy.copy(work) if fault == 'copy' else work
            with monkeypatch.context() as change:
                if fault == 'other_handler':
                    other = SimpleNamespace(server=server, request=request, connection=request)
                    with pytest.raises(ValueError):
                        transport.require_http_request_work(work, handler=other)
                    checked.append(True)
                    return
                if fault == 'socket':
                    change.setattr(handler, 'connection', object())
                elif fault == 'server':
                    change.setattr(handler, 'server', copy.copy(server))
                elif fault == 'executor':
                    change.setattr(server, '_request_executor', object())
                elif fault == 'runtime':
                    change.setattr(server, '_request_drain_runtime', copy.copy(runtime))
                elif fault == 'controller':
                    change.setattr(runtime, 'controller', copy.copy(runtime.controller))
                elif fault == 'closing':
                    runtime.stop_producers()
                with pytest.raises(ValueError):
                    transport.require_http_request_work(selected, handler=handler)
                checked.append(True)

        run_work(server, request, finish)
        assert checked == [True]


def test_copy_context_after_exit_cannot_revive_work_in_same_pool_thread(context, monkeypatch):
    with http_case(context, monkeypatch) as (server, _runtime, _group, request, handler):
        saved = []

        def finish():
            saved.append((copy_context(), transport.capture_http_request_work(handler)))

        run_work(server, request, finish)
        captured, work = saved[0]

        def late():
            assert threading.current_thread() is work.thread
            with pytest.raises(ValueError):
                captured.run(transport.require_http_request_work, work, handler=handler)

        server._request_executor.submit(late).result(timeout=3)


def test_copied_live_context_on_other_thread_is_not_http_work(context, monkeypatch):
    with http_case(context, monkeypatch) as (server, _runtime, _group, request, handler):
        checked = []

        def finish():
            work, captured = transport.capture_http_request_work(handler), copy_context()

            def other():
                with pytest.raises(ValueError):
                    captured.run(transport.require_http_request_work, work, handler=handler)
                checked.append(True)

            thread = threading.Thread(target=other)
            thread.start()
            thread.join(2)
            assert not thread.is_alive()

        run_work(server, request, finish)
        assert checked == [True]


def test_registered_thread_without_original_finish_interval_cannot_capture(context, monkeypatch):
    with http_case(context, monkeypatch) as (server, _runtime, _group, _request, handler):
        def outside():
            with pytest.raises(ValueError):
                transport.capture_http_request_work(handler)
        server._request_executor.submit(outside).result(timeout=3)


def test_original_exception_exit_invalidates_work_and_keeps_existing_handler_policy(context, monkeypatch):
    with http_case(context, monkeypatch) as (server, _runtime, _group, request, handler):
        saved, errors = [], []
        server.handle_error = lambda *_args: errors.append(True)

        def finish():
            saved.append((copy_context(), transport.capture_http_request_work(handler)))
            raise RuntimeError('synthetic original handler error')

        run_work(server, request, finish)
        assert errors == [True] and len(saved) == 1
        with pytest.raises(ValueError):
            saved[0][0].run(transport.require_http_request_work, saved[0][1])


def test_count0_finish_request_keeps_original_zero_source_path(monkeypatch):
    with http_case(None, monkeypatch, count0=True) as (server, _runtime, _group, request, _handler):
        seen = []

        def forbidden(*_args, **_kwargs):
            pytest.fail('Count0 cannot construct HTTP metadata work')

        if hasattr(transport, 'http_request_work_scope'):
            monkeypatch.setattr(transport, 'http_request_work_scope', forbidden)
        run_work(server, request, lambda: seen.append(True))
        assert seen == [True]


def test_root_can_close_during_handler_and_existing_work_immediately_loses_write_source(context, monkeypatch):
    with http_case(context, monkeypatch) as (server, runtime, _group, request, handler):
        checked = []

        def finish():
            work = transport.capture_http_request_work(handler)
            closed = threading.Event()

            def close():
                runtime.stop_producers()
                closed.set()

            thread = threading.Thread(target=close)
            thread.start()
            try:
                assert closed.wait(1), 'Root gate cannot be held across finish_request'
                with pytest.raises(ValueError):
                    transport.require_http_request_work(work, handler=handler)
                checked.append(True)
            finally:
                thread.join(2)
                assert not thread.is_alive()

        run_work(server, request, finish)
        assert checked == [True]


def test_original_baseexception_identity_survives_work_finally(context, monkeypatch):
    class OriginalFault(BaseException):
        pass

    with http_case(context, monkeypatch) as (server, _runtime, _group, request, handler):
        error, saved = OriginalFault(), []

        def finish():
            saved.append((copy_context(), transport.capture_http_request_work(handler)))
            raise error

        with pytest.raises(OriginalFault) as caught:
            run_work(server, request, finish)
        assert caught.value is error and len(saved) == 1
        with pytest.raises(ValueError):
            saved[0][0].run(transport.require_http_request_work, saved[0][1])


def test_readonly_work_query_does_not_wait_on_resource_observer_mutex(context, monkeypatch):
    with http_case(context, monkeypatch) as (server, _runtime, group, request, handler):
        checked = []

        def finish():
            acquired, release, waited = threading.Event(), threading.Event(), threading.Event()

            def holder():
                with group._gate:
                    acquired.set()
                    release.wait(2)

            def watchdog():
                if not release.wait(0.3):
                    waited.set()
                    release.set()

            threads = [threading.Thread(target=holder), threading.Thread(target=watchdog)]
            threads[0].start()
            assert acquired.wait(1)
            threads[1].start()
            try:
                work = transport.capture_http_request_work(handler)
                assert transport.require_http_request_work(work, handler=handler) is work
                assert not waited.is_set()
                checked.append(True)
            finally:
                release.set()
                for thread in threads:
                    thread.join(2)
                    assert not thread.is_alive()

        run_work(server, request, finish)
        assert checked == [True]
