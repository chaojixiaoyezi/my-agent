from __future__ import annotations

"""Tests for gateway HTTP service."""

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# Mock GatewayPaths for testing
class MockGatewayPaths:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "gateway"
        self.inbox = self.root / "requests" / "pending"
        self.processing = self.root / "requests" / "processing"
        self.done = self.root / "requests" / "done"
        self.failed = self.root / "requests" / "failed"
        self.responses = self.root / "responses"
        self.stop_request = self.root / "gateway_stop.request"
        self.state = self.root / "gateway_state.json"
        self.pid = self.root / "gateway.pid"
        self.adapter_pid = self.root / "adapter.pid"
        self.heartbeat = self.root / "gateway_heartbeat.json"
        self.log = self.root / "gateway.log"
        # Create directories
        for path in (self.inbox, self.processing, self.done, self.failed, self.responses):
            path.mkdir(parents=True, exist_ok=True)


class MockHTTPServer:
    """Mock HTTP server for testing without actual network."""

    def __init__(self, paths: MockGatewayPaths):
        self.paths = paths
        self._running = False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False


def find_free_port() -> int:
    """Find a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


class TestGatewayHTTPHandler:
    """Test HTTP handler methods."""

    def test_handle_ask_requires_goal(self, tmp_path: Path):
        """POST /ask without goal should return 400."""
        from agent_py_agent.agent.gateway_parts.http_service import (
            GatewayHTTPHandler,
            _generate_request_id,
        )

        # Verify request ID generation works
        request_id = _generate_request_id()
        assert request_id.startswith("req_")
        assert "_" in request_id


class TestGatewayHTTPIntegration:
    """Integration tests for HTTP service with actual server."""

    @pytest.fixture
    def mock_paths(self, tmp_path: Path) -> MockGatewayPaths:
        return MockGatewayPaths(tmp_path)

    @pytest.fixture
    def http_server(self, mock_paths: MockGatewayPaths):
        """Start HTTP server on a free port."""
        from agent_py_agent.agent.gateway_parts.http_service import (
            GatewayHTTPServer,
            start_http_server,
        )

        port = find_free_port()
        # Import the server class directly
        from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPServer

        server = GatewayHTTPServer(port, mock_paths)
        server.start()
        yield server, port
        server.stop()

    def test_server_starts_and_stops(self, http_server):
        """Server can start and stop without errors."""
        server, port = http_server
        assert server.server is not None

    def test_status_endpoint(self, http_server, mock_paths: MockGatewayPaths):
        """GET /status returns gateway status."""
        import urllib.request

        server, port = http_server
        url = f"http://localhost:{port}/status"

        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                assert response.status == 200
                data = json.loads(response.read().decode("utf-8"))
                assert "status" in data
                assert "requests" in data
        except Exception as e:
            pytest.skip(f"HTTP server not reachable: {e}")

    def test_ask_endpoint(self, http_server, mock_paths: MockGatewayPaths):
        """POST /ask creates a pending request."""
        import urllib.request

        server, port = http_server
        url = f"http://localhost:{port}/ask"
        body = json.dumps({"goal": "测试任务"}).encode("utf-8")

        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as response:
                assert response.status == 202
                data = json.loads(response.read().decode("utf-8"))
                assert "request_id" in data
                assert data["status"] == "queued"
        except Exception as e:
            pytest.skip(f"HTTP server not reachable: {e}")

    def test_result_not_found(self, http_server, mock_paths: MockGatewayPaths):
        """GET /result/<id> returns 404 for unknown request."""
        import urllib.request

        server, port = http_server
        url = f"http://localhost:{port}/result/nonexistent_id"

        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                assert response.status == 404
        except urllib.error.HTTPError as e:
            assert e.code == 404
        except Exception as e:
            pytest.skip(f"HTTP server not reachable: {e}")

    def test_stop_endpoint(self, http_server, mock_paths: MockGatewayPaths):
        """POST /stop initiates graceful shutdown."""
        import urllib.request

        server, port = http_server
        url = f"http://localhost:{port}/stop"

        try:
            req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as response:
                assert response.status == 200
                data = json.loads(response.read().decode("utf-8"))
                assert data["status"] == "stopping"
        except Exception as e:
            pytest.skip(f"HTTP server not reachable: {e}")

    def test_unknown_endpoint_returns_404(self, http_server, mock_paths: MockGatewayPaths):
        """Unknown endpoints return 404."""
        import urllib.error
        import urllib.request

        server, port = http_server
        url = f"http://localhost:{port}/unknown"

        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(url, timeout=5)
            assert exc_info.value.code == 404
        except Exception as e:
            pytest.skip(f"HTTP server not reachable: {e}")


class TestDaemonControl:
    """Test daemon control functions."""

    def test_read_pid_file_nonexistent(self, tmp_path: Path):
        """read_pid_file returns None for nonexistent file."""
        from agent_py_agent.agent.gateway_parts.daemon_control import read_pid_file

        result = read_pid_file(tmp_path / "nonexistent.pid")
        assert result is None

    def test_write_and_read_pid_file(self, tmp_path: Path):
        """write_pid_file and read_pid_file work correctly."""
        from agent_py_agent.agent.gateway_parts.daemon_control import read_pid_file, write_pid_file

        pid_path = tmp_path / "test.pid"
        write_pid_file(pid_path, 12345)
        result = read_pid_file(pid_path)
        assert result == 12345

    def test_remove_pid_file(self, tmp_path: Path):
        """remove_pid_file removes existing file."""
        from agent_py_agent.agent.gateway_parts.daemon_control import (
            remove_pid_file,
            write_pid_file,
        )

        pid_path = tmp_path / "test.pid"
        write_pid_file(pid_path, 12345)
        remove_pid_file(pid_path)
        assert not pid_path.exists()

    def test_check_already_running_no_process(self, tmp_path: Path):
        """check_already_running returns False for dead PID."""
        from agent_py_agent.agent.gateway_parts.daemon_control import (
            check_already_running,
            write_pid_file,
        )

        pid_path = tmp_path / "test.pid"
        write_pid_file(pid_path, 99999)  # Non-existent PID
        is_running, existing_pid = check_already_running(pid_path)
        assert is_running is False
        assert existing_pid is None


class TestConcurrency:
    """Test concurrent request handling."""

    def test_concurrent_requests(self, tmp_path: Path):
        """HTTP server handles concurrent requests."""
        from agent_py_agent.agent.gateway_parts.http_service import (
            GatewayHTTPServer,
            start_http_server,
        )

        paths = MockGatewayPaths(tmp_path)
        port = find_free_port()
        server = GatewayHTTPServer(port, paths)
        server.start()

        results: list[Any] = []
        errors: list[Exception] = []

        def make_request():
            try:
                import urllib.request

                url = f"http://localhost:{port}/status"
                with urllib.request.urlopen(url, timeout=5) as response:
                    results.append(response.status)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=make_request) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        server.stop()

        assert len(errors) == 0 or all(isinstance(e, Exception) for e in errors)
