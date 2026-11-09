"""Idle sockets and incomplete request bodies must not starve the control lane."""
from __future__ import annotations

import concurrent.futures
import socket
import time
from urllib.parse import urlsplit

import pytest

from agent_py_agent.agent.gateway_parts.bounded_http_server import GatewayBoundedHTTPServer
from agent_py_agent.tests.support.ci_authenticated_gateway import authenticated_headers

from .load import durable_receipt, json_request, open_loop
from .report import require_target


# LLM: Saturation uses actual connected sockets, never patches the production executor or limits.
# 函数用途: Exercise status and durable ask acceptance while general HTTP workers are occupied.
def run_cliff(scenario_id, isolated_gateway, slo_data, slo_report, *, slow_body):
    base, home, _proc = isolated_gateway
    scenario = next(row for row in slo_data["scenarios"] if row["id"] == scenario_id)
    profile = scenario["load_profile"]
    endpoint = urlsplit(base)
    sockets = []
    ask_timings, status_timings = {}, {}
    try:
        for _ in range(GatewayBoundedHTTPServer.max_request_workers + profile["extra_connections"]):
            sock = socket.create_connection((endpoint.hostname, endpoint.port), timeout=profile["timeout_ms"] / 1000)
            sockets.append(sock)
            if slow_body:
                headers = authenticated_headers(home)
                wire = f"POST /ask HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {profile['slow_body_bytes']}\r\n"
                wire += "".join(f"{key}: {value}\r\n" for key, value in headers.items())
                sock.sendall((wire + "\r\n{").encode())
        headers = authenticated_headers(home)
        # LLM: The ask and status probes start independently, not after one has freed a worker.
        # 函数用途: Submit an authenticated ask and return its actual durable disposition.
        def submit(_arrival):
            status, body = json_request(base, "/ask", {"prompt": "cliff-fixture", "conversation_id": scenario_id,
                "metadata": {"message_id": scenario_id}, "delivery_intent": "queue"}, headers=headers,
                timeout=profile["timeout_ms"] / 1000, timings=ask_timings)
            return status, body, durable_receipt(body)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            # LLM: Control latency ends at its own response, not when the concurrent ask finishes.
            # 函数用途: Timestamp the status probe independently on the common monotonic clock.
            def status_probe():
                before = time.monotonic()
                status, body = json_request(base, "/status", headers=headers, timeout=profile["timeout_ms"] / 1000,
                                            timings=status_timings)
                return status, body, (time.monotonic() - before) * 1000
            status_future = executor.submit(status_probe)
            samples = open_loop(profile, submit)
            try:
                status_code, status_body, status_ms = status_future.result()
            except (TimeoutError, OSError):
                status_code, status_body, status_ms = 0, {}, profile["timeout_ms"]
        report = slo_report(scenario_id, profile, samples, control_status={"status": status_code,
            "code": status_body.get("error_code"), "latency_ms": status_ms},
            connected_sockets=len(sockets), max_request_workers=GatewayBoundedHTTPServer.max_request_workers,
            transport_stages={"ask": ask_timings, "status": status_timings,
                # queued disposition证明已入账；现有响应没有服务端提交起止，不能用客户端等待冒充fsync。
                "server_receipt_commit_ms": None,
                "server_receipt_commit_coverage": "unavailable_no_server_commit_span",
                "sent_to_headers_coverage": "combined_server_queue_handler_commit_and_transport"})
    finally:
        for sock in sockets:
            sock.close()
    require_target(report["failures"]["count"] == 0, report)
    require_target(status_code == 200 and status_ms <= scenario["targets"]["p99_ms"], report)
    require_target(report["p99_ms"] <= scenario["targets"]["p99_ms"], report)


# LLM: The Stage 1 key is read from scenario data by the isolated subprocess fixture.
# 函数用途: Reproduce the idle-connection cliff through real HTTP requests.
@pytest.mark.slo
@pytest.mark.parametrize("isolated_gateway", ["cliff_idle"], indirect=True)
def test_idle_connections(isolated_gateway, slo_data, slo_report):
    run_cliff("cliff_idle", isolated_gateway, slo_data, slo_report, slow_body=False)


# LLM: Incomplete bodies occupy real general handlers and preserve authentication boundaries.
# 函数用途: Verify slow handlers cannot consume control-lane capacity.
@pytest.mark.slo
@pytest.mark.parametrize("isolated_gateway", ["cliff_slow_handler"], indirect=True)
def test_slow_handlers(isolated_gateway, slo_data, slo_report):
    run_cliff("cliff_slow_handler", isolated_gateway, slo_data, slo_report, slow_body=True)
