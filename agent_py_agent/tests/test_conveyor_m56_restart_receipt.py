import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import ConversationControlCommand
from agent_py_agent.agent.gateway_parts import restart_service
from agent_py_agent.agent.gateway_parts.bounded_http_server import GatewayBoundedHTTPServer
from agent_py_agent.agent.gateway_parts.gateway_state_bounds import ref_count
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServerParams,
    start_http_server,
)
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.restart_control import execute_restart_control
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli import gateway_loops, gateway_process, gateway_restart_handover
from agent_py_agent.cli.parser import build_parser


def test_fast_control_reports_effective_budget(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    admin = SimpleNamespace(
        home_paths=SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main"),
        config=SimpleNamespace(gateway_restart_mode="fast", gateway_restart_fast_timeout_seconds=2,
                               gateway_restart_turn_wait_seconds=300, gateway_restart_drain_timeout_seconds=600),
    )
    reply = execute_restart_control(admin, None, ConversationControlCommand("restart", operation="apply"), paths=paths)
    assert reply.ok and "300" not in reply.message and "2 秒" in reply.message
    receipt = reply.details["restart_receipt"]
    assert receipt["mode"] == "fast"
    assert receipt["budgets"]["drain_seconds"] == 2
    assert receipt["budgets"]["turn_wait_seconds"] == 0


def test_restart_request_receipt_has_startup_phases(tmp_path):
    result = restart_service.submit_restart_request(
        gateway_paths_from_root(tmp_path), target_pid=os.getpid(),
        requester={}, reason="fixture", cooldown_seconds=0,
    )
    receipt = result.get("restart_receipt")
    assert receipt is not None, "scheduled restart lacks a phase receipt"
    assert [phase["name"] for phase in receipt["phases"]] == [
        "import", "config", "db_open", "index_warm", "mcp_spawn", "listener_ready",
        "epoch_transfer", "request_continuation",
    ]


def test_drain_receipt_keeps_blockers(monkeypatch):
    monkeypatch.setattr(restart_service, "wait_until_no_executing_tools", lambda _: False)
    monkeypatch.setattr(restart_service, "executing_tool_count", lambda: 2)
    rows = [{"reason": "tool_pending", "operation_id": str(index)} for index in range(2)]
    monkeypatch.setattr(restart_service, "executing_tool_snapshot", lambda *, limit: rows)
    result = restart_service.run_restart_drain(
        {"request_id": "fixture"}, active_turns=lambda: 1, turn_wait_seconds=0, drain_timeout_seconds=8,
    )
    receipt = result["restart_receipt"]
    assert result["reason"] == "drain_timeout"
    assert receipt["mode"] == "drain" and receipt["budgets"]["drain_seconds"] == 8
    assert ref_count(receipt["blockers"]) == 2
    assert "tool_pending" in restart_service.render_restart_receipt(receipt)


def test_finalize_reference_identity_work_is_linear(monkeypatch):
    size = 10_000
    rows = [{"reason": "model_call_retained", "operation_id": str(index), "scope": ["owner", "thread"]}
            for index in range(size)]
    num_keys = 0
    original = gateway_restart_handover._reference_identity

    def count(row):
        nonlocal num_keys
        num_keys += 1
        return original(row)

    monkeypatch.setattr(gateway_restart_handover, "_reference_identity", count)
    marker = {"restart_mode": "fast", "unresolved_operation_refs": rows}
    cleanup = {"background_blockers": [dict(row) for row in rows]}
    blockers, continuation, models, background = gateway_restart_handover._finalize_reference_sets(
        marker, cleanup, [{"reason": "tool_pending", "operation_id": "retained"}])
    assert len(blockers) == 1 and not background
    assert continuation == models == rows
    assert num_keys <= 26 * size


def test_c128_restart_receipt(tmp_path):
    workers = 128
    entered = Barrier(workers + 1)
    release = Event()

    class ProbeHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.wait(timeout=25)
            assert release.wait(25)
            body = self.path.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    config = AgentConfig(gateway_http_max_request_workers=workers, gateway_http_max_outstanding_requests=256)
    paths = gateway_paths_from_root(tmp_path)
    with GatewayBoundedHTTPServer(("127.0.0.1", 0), ProbeHandler, config=config) as server:
        loop = Thread(target=server.serve_forever, daemon=True)
        loop.start()

        def request(index):
            connection = HTTPConnection(*server.server_address, timeout=30)
            try:
                connection.request("GET", f"/{index}")
                response = connection.getresponse()
                assert response.status == 200
                return response.read().decode()
            finally:
                connection.close()

        try:
            with ThreadPoolExecutor(max_workers=workers) as clients:
                responses = [clients.submit(request, index) for index in range(workers)]
                all_entered = False
                try:
                    entered.wait(timeout=25)
                    all_entered = True
                    observed = server.effective_capacity()
                    write_json_file_atomic(paths.state, {"pid": os.getpid(), "http_capacity": observed})
                    for _ in range(2):
                        scheduled = restart_service.submit_restart_request(
                            paths, target_pid=os.getpid(), requester={}, reason="active clients",
                            cooldown_seconds=0, config=config)
                        assert scheduled["restart_receipt"]["http_capacity"] == observed
                        assert observed["max_request_workers"] == workers
                finally:
                    release.set()
                    if not all_entered:
                        entered.abort()
                assert [future.result(timeout=25) for future in responses] == [f"/{i}" for i in range(workers)]
        finally:
            server.shutdown()
            loop.join(timeout=5)


def test_private_gateway_startup_records_independent_phase_times(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.setenv(restart_service.HOSTING_GATEWAY_PID_ENV, str(os.getpid()))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"my_agent_home: {home}\ngateway_workspace: private-gateway\n"
        "gateway_port: 0\ngateway_worker_process_count: 0\nmodel_backend: echo\n",
        encoding="utf-8",
    )
    args = build_parser().parse_args(["--config", str(config_path), "gateway", "run",
                                      "--workspace-root", str(workspace)])
    from agent_py_agent.cli.common import frozen_gateway_arguments
    from agent_py_agent.cli.gateway_coordinated_run import _cold_gateway_paths

    paths = _cold_gateway_paths(frozen_gateway_arguments(args))
    paths.root.mkdir(parents=True, exist_ok=True)
    old = subprocess.Popen([sys.executable, "-c", "pass"])
    assert old.wait(timeout=5) == 0
    args.after_pid = old.pid
    marker = restart_service.mark_restart_drained(
        paths, {"request_id": "private-restart", "restart_mode": "fast",
                "restart_receipt": restart_service.restart_receipt(mode="fast")},
        old_pid=old.pid, old_process_started_at=0.0, active_turns_at_exit=0,
        cleanup_result={"status": "clean_drain", "handover_allowed": True},
    )
    paths.processing.mkdir(parents=True, exist_ok=True)
    request_id = "gwreq-inflight"
    write_json_file_atomic(paths.processing / f"{request_id}.json", {
        "id": request_id, "kind": "ask", "prompt": "Private smoke fixture", "attempts": 1,
        "status": "processing", "turn_phase": "open", "created_at": time.time() - 5,
        "lease_started_at": time.time() - 10, "save": False,
    })
    listener = {}
    observed = {}
    request_settled = Event()
    execute = gateway_loops._RequestDispatcher._execute

    def observe_request_completion(dispatcher, claim, user_key, conversation_key):
        execute(dispatcher, claim, user_key, conversation_key)
        if claim.request_id == request_id:
            # 原执行器返回时已经释放准入，烟测可安全发出停止请求。
            request_settled.set()

    def bind_ephemeral(agent, actual_paths, port):
        assert port == 0 and actual_paths.root == paths.root
        server = start_http_server(port, actual_paths, params=GatewayHTTPServerParams(agent=agent, bind_host="127.0.0.1"))
        listener["port"] = server.server.server_address[1]
        return server

    def observe_ready(context, **_):
        observed["at_ns"] = time.perf_counter_ns()
        observed["state"] = read_json_file(context.paths.state)
        connection = HTTPConnection("127.0.0.1", listener["port"], timeout=5)
        try:
            connection.request("GET", "/health")
            assert connection.getresponse().status < 500
        finally:
            connection.close()
        assert request_settled.wait(timeout=10), f"{request_id} did not settle within 10 seconds"
        assert (context.paths.terminal / f"{request_id}.json").is_file(), f"{request_id} has no terminal record"
        stop = gateway_process._write_gateway_stop_request(context.paths, reason="private smoke complete",
                                                           target_pid=os.getpid())
        assert stop is not None
        return {"summary": "private listener checked", "stop_request": stop}

    monkeypatch.setattr(gateway_process, "_start_gateway_http", bind_ephemeral)
    monkeypatch.setattr(gateway_process, "_run_gateway_service_loop", observe_ready)
    monkeypatch.setattr(gateway_loops._RequestDispatcher, "_execute", observe_request_completion)
    started_ns = time.perf_counter_ns()
    assert gateway_process.cmd_gateway_run(args) == 0
    receipt = observed["state"]["restart_receipt"]
    assert observed["state"]["restart_request_id"] == marker["request_id"]
    assert receipt["requeued_requests"] == 1
    measured_ms = sum(row["ms"] for row in receipt["phases"])
    observed_ms = (observed["at_ns"] - started_ns) / 1_000_000
    assert abs(measured_ms - observed_ms) / observed_ms <= .05
    phases = {row["name"]: row for row in receipt["phases"]}
    for name in ("import", "config", "db_open", "index_warm", "listener_ready",
                 "epoch_transfer", "request_continuation"):
        assert phases[name]["status"] == "complete" and phases[name]["ms"] > 0
    assert phases["mcp_spawn"] == {"name": "mcp_spawn", "ms": 0, "status": "deferred"}
    assert receipt["http_capacity"]["max_request_workers"] > 0
    print(f"private gateway phase sum {measured_ms:.1f} ms, command-to-ready {observed_ms:.1f} ms")


@pytest.fixture(autouse=True)
def reset_drain():
    yield
    restart_service.reset_restart_drain_state()
