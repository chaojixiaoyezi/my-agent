from __future__ import annotations

import copy
import dataclasses
import http.client
import http.server
import inspect
import io
import os
import pickle
import subprocess
import sys
import threading
import urllib.request
from contextlib import contextmanager

import pytest

from agent_py_agent.agent import owner_change_journal
from agent_py_agent.agent.backends import (
    bounded_call,
    gateway_helpers,
    keepalive_transport,
    transport_timing,
)
from agent_py_agent.agent.backends.base import BackendOptions, ProviderRequestOptions
from agent_py_agent.agent.backends.gateway_helpers import GatewayRequest, _SplitTimeoutOptions
from agent_py_agent.agent.backends.keepalive_transport import (
    KeepAliveRoute,
    KeepAliveTarget,
    _Lease,
)
from agent_py_agent.agent.backends.transport_timing import AttemptTiming, TimedHTTPResponse
from agent_py_agent.agent.common import fast_dataclass
from agent_py_agent.agent.common.direct_socket_io import DirectReadHTTPResponse, DirectSocketIO
from agent_py_agent.agent.common.fast_dataclass import fast_replace
from agent_py_agent.agent.owner_change_journal import OwnerChangeBatch

_BODY = "data: 汉字\r\n\r\ndata: tail".encode()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/peer-close":
            self.close_connection = True
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(_BODY)))
        self.end_headers()
        self.wfile.write(_BODY)

    def log_message(self, *_args):
        pass


@contextmanager
def _serve():
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            yield server.server_address
        finally:
            server.shutdown()
            worker.join(5)


def test_each_connection_uses_the_static_timing_class():
    for host in ("127.0.0.1", "localhost"):
        connection = http.client.HTTPConnection(host)
        transport_timing.instrument_connection(connection, AttemptTiming(lambda _: None), tls=False)
        assert connection.response_class is TimedHTTPResponse


def test_one_hundred_requests_do_not_create_response_classes():
    classes = []
    bases = (DirectReadHTTPResponse, TimedHTTPResponse)
    initial_subclasses = [tuple(type.__subclasses__(base)) for base in bases]
    with _serve() as (host, port):
        for _ in range(100):
            connection = http.client.HTTPConnection(host, port, timeout=5)
            try:
                transport_timing.instrument_connection(connection, AttemptTiming(lambda _: None), tls=False)
                classes.append(connection.response_class)
                connection.request("GET", "/")
                with connection.getresponse() as response:
                    assert response.read() == _BODY
            finally:
                connection.close()
    assert [tuple(type.__subclasses__(base)) for base in bases] == initial_subclasses
    assert {id(cls) for cls in classes} == {id(TimedHTTPResponse)}


def test_timed_reader_keeps_attempt_timing_and_restores_direct_reader():
    clock = iter((0.0, 0.025, 0.075, 0.125, 0.250))
    snapshots = []
    timing = AttemptTiming(snapshots.append, clock=clock.__next__)
    with _serve() as (host, port):
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            transport_timing.instrument_connection(connection, timing, tls=False)
            connection.request("GET", "/")
            with connection.getresponse() as response:
                assert isinstance(response.fp, io.BufferedReader)
                assert type(response.fp.raw) is DirectSocketIO
                assert response.attempt_timing is timing
                assert transport_timing.finish_body_read(response, response.read()) == _BODY
            assert [item["phase"] for item in snapshots] == ["request_send", "first_byte", "body_read", ""]
            assert timing.snapshot() == {
                "phase": "",
                "phase_ms": {"connect": 25.0, "request_send": 50.0, "first_byte": 50.0, "body_read": 125.0},
            }

            transport_timing.instrument_connection(connection, None, tls=False)
            assert connection.response_class is DirectReadHTTPResponse
            connection.request("GET", "/")
            with connection.getresponse() as response:
                assert type(response) is DirectReadHTTPResponse
                assert response.read() == _BODY
        finally:
            connection.close()


def test_timing_context_is_reset_after_read_error():
    timing_context = transport_timing._ATTEMPT_TIMING
    outside = AttemptTiming(lambda _: None)
    token = timing_context.set(outside)
    try:
        with _serve() as (host, port):
            connection = http.client.HTTPConnection(host, port, timeout=5)
            try:
                timing = AttemptTiming(lambda _: None)
                transport_timing.instrument_connection(connection, timing, tls=False)
                connection.request("GET", "/peer-close")
                with pytest.raises(http.client.RemoteDisconnected):
                    connection.getresponse()
                assert timing_context.get() is outside
                assert timing.snapshot()["phase"] == "first_byte"
            finally:
                connection.close()
    finally:
        timing_context.reset(token)


@pytest.mark.parametrize("timed", [False, True])
def test_reused_direct_reader_peeks_and_detects_peer_close(timed):
    timing = AttemptTiming(lambda _: None) if timed else None
    options = _SplitTimeoutOptions(5, 5, None, timing=timing)
    pool = keepalive_transport.KeepAlivePool()
    try:
        with _serve() as (host, port):
            target = KeepAliveTarget(pool, KeepAliveRoute("http", host, port))
            url = f"http://{host}:{port}"
            with keepalive_transport.pooled_urlopen(target, urllib.request.Request(url + "/"), options) as response:
                try:
                    assert type(response.fp.raw) is DirectSocketIO
                    assert response.fp.peek(1).startswith(b"data:")
                    assert response.read() == _BODY
                finally:
                    keepalive_transport.release_pooled_response(response, aborted=False)
            assert pool.idle_count() == 1
            next_options = dataclasses.replace(options, timing=AttemptTiming(lambda _: None) if timed else None)
            with pytest.raises(http.client.RemoteDisconnected) as error:
                keepalive_transport.pooled_urlopen(target, urllib.request.Request(url + "/peer-close"), next_options)
            assert error.value.stale_reused_connection is True
            assert pool.idle_count() == 0
    finally:
        pool.close_all()


def test_unpooled_sse_uses_direct_socket_io():
    with _serve() as (host, port):
        url = f"http://{host}:{port}"
        request = GatewayRequest(url, "key", "/", {}, {}, 5)
        with gateway_helpers._gateway_urlopen(urllib.request.Request(url + "/"), request) as response:
            assert type(response.fp.raw) is DirectSocketIO
            assert list(gateway_helpers._iter_sse_raw_lines(response)) == ["data: 汉字\r".encode(), b"\r", b"data: tail"]


def test_owner_change_journal_script_runs_with_isolated_imports():
    command = [sys.executable, "-I", owner_change_journal.__file__]
    if sys.platform == "linux":
        command.extend((str(os.getpid()), sys.executable, "-c", "print('journal launcher')"))
    result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    if sys.platform == "linux":
        assert (result.returncode, result.stdout) == (0, "journal launcher\n"), result.stderr
    else:
        assert (result.returncode, result.stderr) == (2, "")


def _mro_family(cls):
    yield cls
    for subclass in type.__subclasses__(cls):
        yield from _mro_family(subclass)


def test_explicit_method_calls_match_the_response_and_connection_mro():
    bases = (
        (TimedHTTPResponse, DirectReadHTTPResponse),
        (DirectReadHTTPResponse, http.client.HTTPResponse),
        (gateway_helpers._SplitTimeoutHTTPConnection, http.client.HTTPConnection),
        (gateway_helpers._SplitTimeoutHTTPSConnection, http.client.HTTPSConnection),
        (gateway_helpers._SplitTimeoutHTTPHandler, urllib.request.HTTPHandler),
        (gateway_helpers._SplitTimeoutHTTPSHandler, urllib.request.HTTPSHandler),
        (bounded_call.BoundedCallExitUnconfirmedError, RuntimeError),
        (bounded_call.BoundedCallBusyError, RuntimeError),
    )
    for cls, base in bases:
        for candidate in _mro_family(cls):
            assert candidate.__mro__[candidate.__mro__.index(cls) + 1] is base


def g1_case_backend_options():
    return lambda: BackendOptions("http://127.0.0.1", "key", "model")


def g1_case_provider_request_options():
    return ProviderRequestOptions


def g1_case_gateway_request():
    return lambda: GatewayRequest("http://127.0.0.1", "key", "/", {"q": "hello"}, {}, 5)


def g1_case_split_timeout_options():
    return lambda: _SplitTimeoutOptions(2, 5, None)


def g1_case_keepalive_route():
    return lambda: KeepAliveRoute("http", "127.0.0.1", 80)


def g1_case_keepalive_target():
    route = KeepAliveRoute("http", "127.0.0.1", 80)
    return lambda: KeepAliveTarget(None, route)


def g1_case_keepalive_lease():
    target = KeepAliveTarget(None, KeepAliveRoute("http", "127.0.0.1", 80))
    return lambda: _Lease(target, None)


def g1_case_owner_change_batch():
    return lambda: OwnerChangeBatch("c:1:1", (), True, "complete")


_FACTORIES = (
    g1_case_backend_options,
    g1_case_provider_request_options,
    g1_case_gateway_request,
    g1_case_split_timeout_options,
    g1_case_keepalive_route,
    g1_case_keepalive_target,
    g1_case_keepalive_lease,
    g1_case_owner_change_batch,
)


def _plain_dataclass(cls):
    fields = [(field.name, field.type, copy.copy(field)) for field in dataclasses.fields(cls)]
    namespace = {}
    if hasattr(cls, "__post_init__"):
        namespace["__post_init__"] = cls.__post_init__
    plain_cls = dataclasses.make_dataclass(cls.__name__, fields, namespace=namespace, frozen=True, slots=True)
    plain_cls.__module__ = cls.__module__
    return plain_cls


def _hash_result(instance):
    try:
        return hash(instance)
    except TypeError as exc:
        return str(exc)


@pytest.mark.parametrize("factory", _FACTORIES, ids=lambda factory: factory.__name__.removeprefix("g1_case_"))
def test_frozen_dataclass_matches_plain_dataclass(factory, monkeypatch):
    construct = factory()
    obj = construct()
    cls = type(obj)
    assert hasattr(cls, "__slots__") and cls in fast_dataclass.CONVERTED_CLASSES

    plain_cls = _plain_dataclass(cls)
    assert inspect.signature(cls) == inspect.signature(plain_cls)
    values = {field.name: getattr(obj, field.name) for field in dataclasses.fields(obj)}
    plain = plain_cls(**values)
    assert obj == construct()
    assert repr(obj) == repr(plain)
    assert _hash_result(obj) == _hash_result(plain)
    for field in dataclasses.fields(obj):
        if field.default_factory is not dataclasses.MISSING:
            assert getattr(obj, field.name) is not getattr(construct(), field.name)

    first_field, field_value = next(iter(values.items()))
    with pytest.raises(dataclasses.FrozenInstanceError) as actual:
        setattr(obj, first_field, field_value)
    with pytest.raises(dataclasses.FrozenInstanceError) as expected:
        setattr(plain, first_field, field_value)
    assert str(actual.value) == str(expected.value)

    changes = {first_field: None if field_value is not None else "replacement"}
    changed = fast_replace(obj, **changes)
    assert changed != obj
    assert changed == dataclasses.replace(obj, **changes)
    assert repr(changed) == repr(dataclasses.replace(plain, **changes))
    if hasattr(copy, "replace"):
        assert copy.replace(obj, **changes) == changed
    if cls is GatewayRequest:
        with pytest.raises(ValueError) as actual:
            fast_replace(obj, max_retries=-1)
        with pytest.raises(ValueError) as expected:
            dataclasses.replace(plain, max_retries=-1)
        assert str(actual.value) == str(expected.value)
    with pytest.raises(TypeError) as actual:
        fast_replace(obj, unknown_field=True)
    with pytest.raises(TypeError) as expected:
        dataclasses.replace(plain, unknown_field=True)
    assert str(actual.value) == str(expected.value)

    data = pickle.dumps(obj)
    assert pickle.loads(data) == obj
    with monkeypatch.context() as patch:
        patch.setattr(sys.modules[cls.__module__], cls.__name__, plain_cls)
        assert pickle.loads(data) == plain
        plain_data = pickle.dumps(plain)
    assert pickle.loads(plain_data) == obj
