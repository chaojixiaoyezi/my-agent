"""Native NDJSON observers must not retain the general HTTP worker pool."""
from __future__ import annotations

import concurrent.futures
import http.client
import json
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest

from agent_py_agent.agent.gateway_parts.bounded_http_server import GatewayBoundedHTTPServer
from agent_py_agent.tests.support.ci_authenticated_gateway import authenticated_headers

from .load import Sample, json_request
from .report import require_target


# LLM: Native stream requests and snapshots travel through the real authenticated route.
# 函数用途: Keep the configured number of business streams open and probe the control plane.
@pytest.mark.slo
@pytest.mark.parametrize("isolated_gateway", ["streams_control"], indirect=True)
def test_streams_control(isolated_gateway, slo_data, slo_report):
    base, home, _proc = isolated_gateway
    scenario = next(row for row in slo_data["scenarios"] if row["id"] == "streams_control")
    profile, endpoint = scenario["load_profile"], urlsplit(base)
    headers = authenticated_headers(home)
    sockets, snapshots, errors = [], [], []
    condition, close = threading.Condition(), threading.Event()
    # LLM: A first NDJSON snapshot proves the socket reached the native stream, not just the backlog.
    # 函数用途: Open one actual observer and retain it until all control probes finish.
    def observer(index):
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=profile["read_seconds"])
        sockets.append(connection)
        try:
            connection.request("POST", "/client/business/stream", json.dumps({"request_id": f"observer-{index}", "filter": {}}),
                               {"Content-Type": "application/json", **headers})
            response = connection.getresponse()
            if response.status != 200:
                if response.status != 503:
                    raise RuntimeError(f"unexpected native stream protocol response: {response.status}: {response.read()!r}")
                raise OSError("native stream capacity exhausted")
            frame = json.loads(response.readline())
            with condition:
                snapshots.append(frame)
                condition.notify_all()
            close.wait(slo_data["fixture"]["condition_seconds"])
        except (OSError, http.client.HTTPException) as error:
            with condition:
                errors.append(type(error).__name__)
                condition.notify_all()
        finally:
            connection.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=profile["streams"]) as executor:
        futures = [executor.submit(observer, index) for index in range(profile["streams"])]
        try:
            with condition:
                condition.wait_for(lambda: len(snapshots) >= min(profile["streams"], GatewayBoundedHTTPServer.max_request_workers),
                                   slo_data["fixture"]["condition_seconds"])
            samples, responses = [], []
            for path, body in (("/status", None), ("/control", {"command": "/stop", "conversation_id": "control-fixture", "metadata": {"message_id": "stop-fixture"}}),
                               ("/ask", {"prompt": "steer-fixture", "conversation_id": "control-fixture", "metadata": {"message_id": "steer-fixture"}})):
                before = time.monotonic()
                sample = Sample(before, profile["timeout_ms"], sent_at=before)
                try:
                    status, response = json_request(base, path, body, headers=headers, timeout=profile["timeout_ms"] / 1000)
                    responses.append({"path": path, "status": status, "body": response, "latency_ms": (time.monotonic() - before) * 1000})
                    sample.status = status
                    from .load import durable_receipt
                    sample.request_id = response.get("request_id", "")
                    sample.receipt = durable_receipt(response)
                    if sample.receipt:
                        sample.accepted_at = time.monotonic()
                    else:
                        sample.unconfirmed, sample.code = True, response.get("error_code", "NO_DURABLE_ASK_RECEIPT")
                except (OSError, http.client.HTTPException):
                    sample.timed_out, sample.code = True, "CONTROL_TIMEOUT"
                    responses.append({"path": path, "status": 0, "latency_ms": profile["timeout_ms"]})
                if path == "/ask":
                    samples.append(sample)
            report = slo_report("streams_control", profile, samples, controls=responses,
                opened_streams=len(snapshots), stream_errors=errors, initial_frames=snapshots,
                stop_acknowledged=next((row for row in responses if row["path"] == "/control"), None), exit_proof=None)
        finally:
            close.set()
            for connection in sockets:
                if connection.sock:
                    try:
                        connection.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                connection.close()
            for future in futures:
                future.result(timeout=slo_data["fixture"]["teardown_seconds"])
    require_target(len(snapshots) == profile["streams"], report)
    require_target(all(row["status"] in {200, 202} and row["latency_ms"] <= scenario["targets"]["p99_ms"] for row in responses), report)
    require_target(report["failures"]["count"] == 0, report)
