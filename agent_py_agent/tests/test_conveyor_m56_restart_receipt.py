import json
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


# ---- B4-M56-P1-01 / P1-02: shared admission outcome and receipt-derived terminal wait ----

def _admin_agent(**config):
    values = {"gateway_restart_cooldown_seconds": 0, "gateway_restart_mode": "graceful",
              "gateway_restart_fast_timeout_seconds": 2, "gateway_restart_turn_wait_seconds": 0,
              "gateway_restart_drain_timeout_seconds": 0, **config}
    return SimpleNamespace(
        home_paths=SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main"),
        config=SimpleNamespace(**values),
    )


def _incumbent(paths, operation, pid):
    first = restart_service.submit_restart_request(paths, target_pid=pid, requester={"kind": "cli"},
                                                   reason="incumbent", cooldown_seconds=0, operation=operation)
    assert first["status"] == "scheduled"
    return read_json_file(restart_service.restart_request_path(paths))


def _assert_untouched(paths, before):
    after = read_json_file(restart_service.restart_request_path(paths))
    assert after == before, "a refused request must not enrol a requester or change the incumbent"


class _SimulatedClock:
    """Deterministic monotonic clock for the terminal wait loop; sleep advances time instead of blocking."""

    def __init__(self, on_tick=None, limit=100_000.0):
        self.now, self.on_tick, self.limit, self.sleeps = 0.0, on_tick, limit, 0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000.0 + self.now

    def sleep(self, seconds):
        self.now += float(seconds)
        self.sleeps += 1
        assert self.now < self.limit, "wait loop did not reach a terminal outcome"
        if self.on_tick is not None:
            self.on_tick(self.now)


def _forbid_sleep(monkeypatch):
    clock = _SimulatedClock(limit=0.1)
    monkeypatch.setattr(gateway_restart_handover, "time", clock)
    return clock


@pytest.mark.parametrize("incumbent", ["stop_for_update", "restart"])
def test_conflicting_operations_are_refused_immediately_at_every_entry(tmp_path, monkeypatch, capsys, incumbent):
    from agent_py_agent.agent.tooling.gateway_restart_tool import RestartGatewayTool

    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir(parents=True)
    pid = os.getpid()
    before = _incumbent(paths, incumbent, pid)
    monkeypatch.setattr(gateway_restart_handover, "get_running_pid", lambda _path: pid)
    monkeypatch.setattr(gateway_restart_handover, "is_pid_alive", lambda _pid: True)
    monkeypatch.setattr(gateway_restart_handover, "refuse_stopping_hosting_gateway", lambda _pid: False)
    clock = _forbid_sleep(monkeypatch)
    agent = _admin_agent()

    if incumbent == "stop_for_update":
        reply = execute_restart_control(agent, SimpleNamespace(thread_id="th-1"),
                                        ConversationControlCommand("restart", operation="apply"), paths=paths)
        assert (reply.ok, reply.error_code, reply.request_id) == (False, "RUNTIME_CONFLICT", "")
        assert reply.details["incumbent_request_id"] == before["request_id"]
        assert reply.details["incumbent_operation"] == "stop_for_update"
        assert reply.details["side_effect_state"] == "not_started"
        _assert_untouched(paths, before)

        monkeypatch.setenv(restart_service.HOSTING_GATEWAY_PID_ENV, str(pid))
        tool_agent = SimpleNamespace(**vars(agent), tools=SimpleNamespace(owner_type="main_agent"),
                                     conversation_store=None)
        monkeypatch.setattr("agent_py_agent.agent.tooling.gateway_restart_tool.gateway_paths", lambda _a: paths)
        monkeypatch.setattr("agent_py_agent.agent.tooling.gateway_restart_tool.current_subagent_run_id", lambda _a: "")
        monkeypatch.setattr("agent_py_agent.agent.tooling.gateway_restart_tool.current_conversation_task_attributes",
                            lambda _a: {"conversation_thread_id": "th-2"})
        outcome = RestartGatewayTool(tool_agent).execute({"reason": "fixture"})
        body = json.loads(outcome.output)
        assert (outcome.ok, outcome.error_code, outcome.effect_outcome) == (False, "RUNTIME_CONFLICT", "not_started")
        assert body["status"] == "conflict" and body["incumbent_request_id"] == before["request_id"]
        assert "restart_receipt" not in body and "request" not in body
        _assert_untouched(paths, before)
        monkeypatch.delenv(restart_service.HOSTING_GATEWAY_PID_ENV)

        assert gateway_restart_handover.safe_restart_from_cli(SimpleNamespace(config=agent.config), paths) == 2
    else:
        assert gateway_restart_handover.safe_stop_for_update(SimpleNamespace(config=agent.config), paths, pid) == 2
    err = capsys.readouterr().err
    assert before["request_id"] in err and "RUNTIME_CONFLICT" in err and "not_started" in err
    assert clock.sleeps == 0, "a conflict must not start waiting on the incumbent's request"
    _assert_untouched(paths, before)


def test_same_operation_coalesces_on_the_original_request(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir(parents=True)
    before = _incumbent(paths, "restart", os.getpid())
    reply = execute_restart_control(_admin_agent(), SimpleNamespace(thread_id="th-1"),
                                    ConversationControlCommand("restart", operation="apply"), paths=paths)
    assert reply.ok and reply.request_id == before["request_id"]
    after = read_json_file(restart_service.restart_request_path(paths))
    assert after["operation"] == "restart" and len(after["additional_requesters"]) == 1


def _start_cli(monkeypatch, paths, pid, on_tick):
    clock = _SimulatedClock(on_tick)
    monkeypatch.setattr(gateway_restart_handover, "time", clock)
    monkeypatch.setattr(gateway_restart_handover, "refuse_stopping_hosting_gateway", lambda _pid: False)
    return clock


@pytest.mark.parametrize("outcome", ["successor_ready", "cancelled"])
def test_unlimited_drain_keeps_the_terminal_restart_on_its_request(tmp_path, monkeypatch, capsys, outcome):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir(parents=True)
    current = {"pid": 4242}
    monkeypatch.setattr(gateway_restart_handover, "get_running_pid", lambda _path: current["pid"])
    monkeypatch.setattr(gateway_restart_handover, "is_pid_alive", lambda pid: pid == current["pid"])
    monkeypatch.setattr(gateway_restart_handover, "build_process_identity", lambda _pid: {"start_time": None})

    def tick(now):
        if now < 5_000:
            return  # a tool is still executing well past the former 3,720 s substitute
        request = read_json_file(restart_service.restart_request_path(paths))
        if outcome == "cancelled":
            write_json_file_atomic(paths.state, {"status": "running", "pid": 4242, "restart_drain": {
                "request_id": request["request_id"], "phase": "cancelled", "reason": "stop_requested",
                "service_cleanup_started": False}})
        else:
            current["pid"] = 5353
            write_json_file_atomic(paths.state, {"status": "running", "pid": 5353})

    clock = _start_cli(monkeypatch, paths, 4242, tick)
    code = gateway_restart_handover.safe_restart_from_cli(_admin_agent(), paths)
    captured = capsys.readouterr()
    assert clock.now >= 5_000
    assert "超时" not in captured.err
    if outcome == "cancelled":
        assert code == 2 and "排空已取消" in captured.err
    else:
        assert code == 0 and "4242 → 5353" in captured.out


def test_unlimited_drain_bounds_only_the_successor_phase(tmp_path, monkeypatch, capsys):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir(parents=True)
    alive = {"old": True}
    monkeypatch.setattr(gateway_restart_handover, "get_running_pid", lambda _path: 4242)
    monkeypatch.setattr(gateway_restart_handover, "is_pid_alive", lambda _pid: alive["old"])
    exited_at = {}

    def tick(now):
        if now >= 6_000 and alive["old"]:
            alive["old"] = False
            exited_at["t"] = now

    clock = _start_cli(monkeypatch, paths, 4242, tick)
    assert gateway_restart_handover.safe_restart_from_cli(_admin_agent(), paths) == 2
    assert "超时" in capsys.readouterr().err
    assert 120 <= clock.now - exited_at["t"] <= 121


@pytest.mark.parametrize("config, expected", [
    ({"gateway_restart_turn_wait_seconds": 5, "gateway_restart_drain_timeout_seconds": 10}, 135.0),
    ({"gateway_restart_mode": "fast", "gateway_restart_turn_wait_seconds": 300,
      "gateway_restart_drain_timeout_seconds": 0}, 122.0),
])
def test_finite_and_fast_budgets_still_time_out(tmp_path, monkeypatch, capsys, config, expected):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir(parents=True)
    monkeypatch.setattr(gateway_restart_handover, "get_running_pid", lambda _path: 4242)
    monkeypatch.setattr(gateway_restart_handover, "is_pid_alive", lambda _pid: True)
    clock = _start_cli(monkeypatch, paths, 4242, None)
    assert gateway_restart_handover.safe_restart_from_cli(_admin_agent(**config), paths) == 2
    assert "超时" in capsys.readouterr().err
    assert expected <= clock.now <= expected + 0.5


@pytest.mark.parametrize("outcome", ["clean_exit", "cancelled"])
def test_unlimited_drain_keeps_update_stop_on_its_request(tmp_path, monkeypatch, capsys, outcome):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.root.mkdir(parents=True)
    pid = 4242
    identity = {"pid": pid, "start_time": 11.0}
    alive = {"value": True}
    monkeypatch.setattr(gateway_restart_handover, "is_pid_alive", lambda _pid: alive["value"])
    monkeypatch.setattr(gateway_restart_handover, "build_process_identity", lambda _pid: identity)

    def tick(now):
        if now < 5_000 or not alive["value"]:
            return
        request = read_json_file(restart_service.restart_request_path(paths))
        if outcome == "cancelled":
            write_json_file_atomic(paths.state, {"pid": pid, "restart_drain": {
                "request_id": request["request_id"], "phase": "cancelled", "reason": "drain_timeout"}})
            return
        write_json_file_atomic(restart_service.restart_completed_path(paths), {
            "request_id": request["request_id"], "old_pid": pid, "operation": "stop_for_update",
            "old_process_identity": identity,
            "cleanup_result": {"status": "clean_drain", "handover_allowed": True}})
        alive["value"] = False

    clock = _start_cli(monkeypatch, paths, pid, tick)
    code = gateway_restart_handover.safe_stop_for_update(_admin_agent(), paths, pid)
    err = capsys.readouterr().err
    assert clock.now >= 5_000 and "超时" not in err
    assert code == (0 if outcome == "clean_exit" else 2)
