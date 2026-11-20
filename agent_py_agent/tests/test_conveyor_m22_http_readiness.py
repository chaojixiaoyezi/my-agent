"""M22 的真实连接就绪、运行容量和阶段耗时回归。"""
from __future__ import annotations

import json
import random
import sys
import sysconfig
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.direct_socket_io import DirectSocketIO
from agent_py_agent.agent.common.fast_dataclass import check_slots_compat
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import http_lanes, request_execution
from agent_py_agent.agent.gateway_parts.bounded_http_server import GatewayBoundedHTTPServer
from agent_py_agent.agent.gateway_parts.http_body_readiness import (
    _parse_request_length,
    complete_request_length,
    scan_request_length,
)
from agent_py_agent.agent.gateway_parts.http_lanes import classify_peeked_request, lane_queue_ms
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPHandler,
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.gateway_process import _build_run_payload, _build_run_state
from agent_py_agent.cli.local_commands import _gateway_status_from_files
from agent_py_agent.cli.local_status_payload import _gateway_payload
from agent_py_agent.tests.test_coordinator_control_forward import cold as cold
from agent_py_agent.tests.test_gateway_c_http_wait import request as http_request
from agent_py_agent.tests.test_s1w1_http_lanes import (
    Handler,
    connect,
    eventually,
    request,
    response,
)
from agent_py_agent.tests.test_s1w1_http_lanes import server as server


def g1_case_lane_classification():
    prefix = b"POST /client/turn-stop HTTP/1.1\r\nHost: localhost\r\n\r\n"

    def classify():
        return classify_peeked_request(prefix)

    return classify


def g1_case_http_server_params():
    return GatewayHTTPServerParams


def test_readiness_delay_does_not_consume_queue_timeout(server):
    with connect(server) as client:
        time.sleep(server.request_queue_timeout + 0.04)
        client.sendall(b"GET /status HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert b"200 OK" in response(client)


def _make_server(workers):
    return GatewayBoundedHTTPServer(
        ("127.0.0.1", 0), Handler, control_lane_workers=2,
        config=SimpleNamespace(gateway_http_max_request_workers=workers),
    )


@pytest.mark.parametrize(("workers", "shards"), [(128, 4), (256, 8)])
def test_readiness_shard_count(workers, shards):
    with _make_server(workers) as listener:
        actual = listener._lanes.shards
        assert len(actual) == shards
        assert {shard.thread.name for shard in actual} == {
            f"gateway-http-readiness-{i}" for i in range(shards)
        }
        assert all(shard.thread.is_alive() for shard in actual)
    assert all(not shard.thread.is_alive() and shard.wake_read.fileno() == -1
               and shard.wake_write.fileno() == -1 for shard in actual)


def test_run_state_reflects_live_http_capacities(tmp_path):
    with _make_server(128) as listener:
        request = SimpleNamespace(
            context=SimpleNamespace(
                paths=SimpleNamespace(root=tmp_path),
                agent=SimpleNamespace(config=SimpleNamespace(), subagents=SimpleNamespace(workspace=tmp_path)),
                config_path=tmp_path / "config", coordinator_worker_count=0,
                log_start_offset_bytes=0, process_identity={}, process_started_at=1,
            ),
            requeued=0, failed=0, http_port=listener.server_port,
            http_server=SimpleNamespace(server=listener),
        )
        capacity = _build_run_state(request, 1)["http_capacity"]
        assert capacity["max_request_workers"] == 128
        assert capacity["configured_max_request_workers"] == 128
        assert capacity["max_outstanding_requests"] == listener.max_outstanding_requests
        assert capacity["readiness_shards"] == 4
        assert capacity["control_lane_workers"] == 2
        assert capacity["queue_timeout_seconds"] == listener.request_queue_timeout
        assert "POST /client/turn-stop" in capacity["stop_lane_routes"]
        assert capacity["gil_enabled"] is getattr(sys, "_is_gil_enabled", lambda: True)()
        assert capacity["free_threaded_build"] is bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
        assert _build_run_payload(request, 1)["http_capacity"] == capacity
        paths = gateway_paths_from_root(tmp_path / "gateway")
        paths.root.mkdir(exist_ok=True)
        paths.state.write_text(json.dumps({"http_capacity": capacity}))
        *_, runtime = _gateway_status_from_files(request.context.agent, paths, True)
        status_context = SimpleNamespace(
            gateway_status="running", pid=1, alive=True, heartbeat_age=0, request_counts={},
            archive_request_counts={}, paths=paths, unidentified_stale_attempts=0,
            surviving_background_sessions=0, gateway_state_load_error=None,
            gateway_heartbeat_load_error=None, http_capacity=runtime["http_capacity"],
        )
        assert _gateway_payload(status_context)["http_capacity"] == capacity
        request.http_port = 0
        assert _build_run_state(request, 1)["http_capacity"] == {}


def test_persisted_lane_delay_goes_into_its_request_stages(tmp_path, monkeypatch):
    monkeypatch.setattr(request_execution, "audit_request_processing", lambda *_: None)
    monkeypatch.setattr(request_execution, "gateway_paths", lambda _: None)
    monkeypatch.setattr(request_execution, "gateway_response_path", lambda _, request_id: tmp_path / request_id)
    for request_id, delay in (("first", 17), ("second", 348)):
        path = tmp_path / f"{request_id}.json"
        path.write_text(json.dumps({"id": request_id, "kind": "ask", "lane_queue_ms": delay}))
        context = request_execution._prepare_gateway_request_context(None, path)
        assert context["stage_timings"]["lane_queue_ms"] == delay


def test_byte_scanner_matches_old_parser_on_100000_mutated_prefixes():
    rng = random.Random(22)
    template = (
        b"POST /ask HTTP/1.1\r\nHost: localhost\r\nContent-Length: \t00002\r\n"
        b"Connection: close\r\n\r\n{}"
    )
    samples = (
        template, template.replace(b"HTTP/1.1", b"HTTP/1.0"),
        template.replace(b"\r\n", b"\n"),
        template.replace(b"Content-Length", b"Expect"),
        template.replace(b"Connection", b"Transfer-Encoding"),
        template.replace(b"Host: localhost", b"Content-Length: 2"),
        template.replace(b"Host: localhost", b"Host: localhost\r\n folded"),
        template.replace(b"Host: localhost\r\n", b""),
        template.replace(b"Host: localhost\r\n", b"X: y\r\n" * 100),
        b"GET /status HTTP/1.1\r\n\r\n",
        b"POST / HTTP/1.1\r\nContent-Length: " + b"0" * 5000 + b"2\r\n\r\n{}",
    )
    for i in range(100_000):
        prefix = bytearray(rng.choice(samples))
        if i % 4 == 1:
            prefix = bytearray(rng.randbytes(rng.randrange(96)))
        elif i % 4 >= 2:
            start = rng.randrange(len(prefix))
            prefix[start:start + rng.randrange(4)] = rng.randbytes(rng.randrange(5))
            if i % 8 == 3:
                del prefix[rng.randrange(len(prefix)):]
        if i % 2 == 0:
            prefix = bytes(prefix)
        offset = rng.randrange(5) if i % 5 else rng.randrange(len(prefix) + 1)
        options = {"single_request": bool(i % 3), "search_from": offset}
        slow = _parse_request_length(prefix, **options)
        fast = scan_request_length(prefix, **options)
        assert fast == 0 or fast == slow, (i, prefix, fast, slow)
        assert complete_request_length(prefix, **options) == slow, (i, prefix)


def test_500_status_requests_while_all_business_workers_are_busy():
    capacity = 512
    workers = 128
    listener = GatewayBoundedHTTPServer(
        ("127.0.0.1", 0), Handler, control_lane_workers=2,
        pending_connection_limit=capacity + workers,
        config=SimpleNamespace(
            gateway_http_max_request_workers=workers,
            gateway_http_max_outstanding_requests=capacity,
            gateway_http_listen_backlog=capacity,
        ),
    )
    listener.release = threading.Event()
    listener.handled = threading.Event()
    listener.test_lock = threading.Lock()
    listener.started = 0
    thread = threading.Thread(target=listener.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        with ExitStack() as sockets:
            polls = [sockets.enter_context(connect(listener)) for _ in range(500)]
            for _ in range(workers):
                sockets.enter_context(request(listener, "POST", "/ask"))
            eventually(lambda: listener.started == workers, timeout=2)
            started = time.monotonic()
            for client in polls:
                client.sendall(b"GET /status HTTP/1.1\r\nHost: localhost\r\n\r\n")
            assert sum(b"200 OK" in response(client) for client in polls) == 500
            assert time.monotonic() - started < listener.request_queue_timeout
    finally:
        listener.release.set()
        listener.shutdown()
        listener.server_close()
        thread.join()
    assert all(not shard.thread.is_alive() for shard in listener._lanes.shards)


def test_loopback_request_delays_are_isolated_and_first_receipt_wins(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    agent = SimpleAgent(AgentConfig(my_agent_home=str(home), model_backend="echo",
                                   enable_plugins=False, gateway_http_control_lane_workers=2), tmp_path / "workspace")
    paths = gateway_paths_from_root(tmp_path / "gateway")
    service = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent))
    service.start()
    assert service.server is not None
    router = service.server._lanes
    injection_time = ContextVar("fixture_lane_start", default=None)
    outcomes = []
    asks_entered = threading.Barrier(2)
    start_at = time.monotonic
    monkeypatch.setattr(http_lanes, "time", SimpleNamespace(monotonic=lambda: injection_time.get() or start_at()))
    original_run = router._run_ticket
    original_setup = GatewayHTTPHandler.setup
    readers = []

    def check_setup(handler):
        original_setup(handler)
        readers.append((handler.rbufsize, type(handler.rfile.raw)))

    def run(ticket):
        ticket._ready.wait()
        wire = bytes(ticket.request._prefix)
        delay_ms = next((delay for delay in (17, 348, 701) if f"X-Fixture: {delay}\r\n".encode() in wire), 0)
        if delay_ms in (17, 348):
            asks_entered.wait(timeout=2)
        token = injection_time.set(ticket.enqueued_at + (delay_ms + 0.1) / 1000)
        try:
            original_run(ticket)
            outcomes.append(lane_queue_ms.get())
        finally:
            injection_time.reset(token)

    monkeypatch.setattr(GatewayHTTPHandler, "setup", check_setup)
    monkeypatch.setattr(router, "_run_ticket", run)

    def ask(name, delay):
        body = {"goal": name, "delivery_intent": "queue", "metadata": {"message_id": name}}
        return http_request(service, "POST", "/ask", body,
                            headers={"X-Channel": "local", "X-Fixture": str(delay)})

    try:
        with ThreadPoolExecutor(max_workers=4) as clients:
            first = clients.submit(ask, "first", 17)
            second = clients.submit(ask, "second", 348)
            polls = [clients.submit(http_request, service, "GET", "/status") for _ in range(2)]
            records = [first.result(), second.result()]
            for poll in polls:
                status, payload = poll.result()
                assert status == 200 and "lane_queue_ms" not in payload
        assert {code for code, _ in records} == {202}
        stored = {}
        for expected, (_, receipt) in zip((17, 348), records, strict=True):
            request_id = receipt["request_id"]
            path = paths.inbox / f"{request_id}.json"
            stored[expected] = path.read_bytes()
            context = request_execution._prepare_gateway_request_context(agent, path)
            assert context["request_id"] == request_id
            assert context["stage_timings"]["lane_queue_ms"] == expected
        replay_code, replay = ask("first", 701)
        assert replay_code in (200, 202) and replay["request_id"] == records[0][1]["request_id"]
        assert {path.read_bytes() for path in paths.inbox.glob("*.json")} == set(stored.values())
        eventually(lambda: len(outcomes) == 5)
        assert outcomes == [None] * 5
        assert readers == [(-1, DirectSocketIO)] * 5
    finally:
        service.stop()


@pytest.mark.parametrize("cold", [("feishu", True)], indirect=True)
def test_cold_coordinator_retains_source_for_authenticated_turn_stop(cold):
    assert cold.server.agent is None
    body = {"message_id": "readiness-stop", "scope": "session", "conversation_id": "control-session"}
    code, _ = http_request(cold.server, "POST", "/client/turn-stop", body, credential=False)
    assert code == 403
    code, result = http_request(cold.server, "POST", "/client/turn-stop", body)
    assert code == 202 and result["target_thread_ids"] == [cold.thread.thread_id], result
    assert not cold.constructions
    runtime = cold.server.drain_runtime
    listener = cold.server.server
    assert runtime._resources["http/requests"].owner is listener._request_executor
    assert runtime._resources["http/readiness/0"].owner is listener._lanes.shards[0].thread


def test_http_params_keep_dataclass_value_semantics():
    @dataclass(frozen=True)
    class PlainParams:
        cross_channel: object = None
        admin_query: object = None
        auth_middleware: object = None
        bind_host: str = "127.0.0.1"
        agent: object = None
        control_plane: object = None
        drain_runtime: object = None

    assert check_slots_compat(GatewayHTTPServerParams) == []
    expected = PlainParams(bind_host="localhost")
    live = GatewayHTTPServerParams(bind_host="localhost")
    assert asdict(live) == asdict(expected)
    assert hash(live) == hash(expected)
    assert live == GatewayHTTPServerParams(bind_host="localhost")
    assert repr(live) == repr(expected).replace(PlainParams.__qualname__, "GatewayHTTPServerParams", 1)
