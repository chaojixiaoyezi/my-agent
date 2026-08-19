from __future__ import annotations

"""Runtime error visibility tests for gateway HTTP diagnostics."""

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPServer
from agent_py_agent.tests.test_gateway_http import MockGatewayPaths, find_free_port


@pytest.fixture
def mock_paths(tmp_path: Path) -> MockGatewayPaths:
    return MockGatewayPaths(tmp_path)


@pytest.fixture
def http_server(mock_paths: MockGatewayPaths):
    port = find_free_port()
    server = GatewayHTTPServer(port, mock_paths)
    server.start()
    yield server, port
    server.stop()


def _read_status(port: int) -> dict:
    url = f"http://localhost:{port}/status"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            raw = response.read()
    except Exception as exc:
        pytest.skip(f"HTTP server not reachable: {exc}")
    return json.loads(raw.decode("utf-8"))


def _read_result(port: int, request_id: str) -> tuple[int, dict]:
    url = f"http://localhost:{port}/result/{request_id}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
    except Exception as exc:
        pytest.skip(f"HTTP server not reachable: {exc}")


def test_serve_records_structured_runtime_error(mock_paths: MockGatewayPaths):
    """A crashed HTTP serve loop should not disappear as a silent pass."""

    class BrokenServer:
        def serve_forever(self) -> None:
            raise RuntimeError("serve crashed")

    server = GatewayHTTPServer(0, mock_paths)
    server.server = BrokenServer()

    server._serve()

    assert server.last_error_report is not None
    assert server.last_error_report["context"] == "gateway.http_server.serve"
    assert "serve crashed" in server.last_error_report["message"]
    state = json.loads(mock_paths.state.read_text(encoding="utf-8"))
    assert state["status"] == "http_server_error"
    assert state["server_error"]["context"] == "gateway.http_server.serve"


def test_status_endpoint_includes_server_error(
    http_server,
    mock_paths: MockGatewayPaths,
):
    """GET /status exposes structured HTTP server errors from state."""
    server, port = http_server
    mock_paths.state.write_text(
        json.dumps(
            {
                "status": "http_server_error",
                "server_error": {
                    "context": "gateway.http_server.serve",
                    "message": "serve crashed",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    data = _read_status(port)

    assert data["server_error"]["context"] == "gateway.http_server.serve"


def test_status_endpoint_reports_bad_state_file(
    http_server,
    mock_paths: MockGatewayPaths,
):
    """GET /status should not hide a corrupted state file as unknown state."""
    server, port = http_server
    mock_paths.state.write_text("{bad json", encoding="utf-8")

    data = _read_status(port)

    assert data["state_load_error"]["context"] == "gateway.http_state.read"


def test_result_pending_state_reports_bad_request_file(
    http_server,
    mock_paths: MockGatewayPaths,
):
    """GET /result should not hide a bad pending request file as normal queued."""
    server, port = http_server
    (mock_paths.inbox / "broken.json").write_text("{bad json", encoding="utf-8")

    status, data = _read_result(port, "broken")

    assert status == 202
    assert data["request_load_error"]["context"] == "gateway.http_pending.read"


def test_result_ignores_bad_standalone_response_file(
    http_server,
    mock_paths: MockGatewayPaths,
):
    """GET /result must not promote a standalone response projection to terminal."""
    server, port = http_server
    (mock_paths.responses / "broken.json").write_text("{bad json", encoding="utf-8")

    status, data = _read_result(port, "broken")

    assert status == 404
    assert data["request_id"] == "broken"


def test_result_reports_bad_canonical_terminal_file(
    http_server,
    mock_paths: MockGatewayPaths,
):
    """GET /result exposes corruption only from the canonical terminal authority."""
    server, port = http_server
    (mock_paths.terminal / "broken.json").write_text("{bad json", encoding="utf-8")

    status, data = _read_result(port, "broken")

    assert status == 500
    assert data["result_load_error"]["context"] == "gateway.http_terminal_archive.read"


def test_result_rejects_cross_request_canonical_terminal(
    http_server,
    mock_paths: MockGatewayPaths,
) -> None:
    _server, port = http_server
    (mock_paths.terminal / "request-a.json").write_text(
        json.dumps(
            {
                "schema_version": "gateway_terminal_request.v1",
                "id": "request-b",
                "terminal_response": {
                    "id": "request-b",
                    "ok": True,
                    "response": "must not escape another request",
                },
            }
        ),
        encoding="utf-8",
    )

    status, data = _read_result(port, "request-a")

    assert status == 500
    assert data["result_load_error"]["category"] == "data_corruption"
    assert "response" not in data
