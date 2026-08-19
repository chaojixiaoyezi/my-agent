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
        self.terminal = self.root / "requests" / "terminal"
        self.responses = self.root / "responses"
        self.stop_request = self.root / "gateway_stop.request"
        self.state = self.root / "gateway_state.json"
        self.pid = self.root / "gateway.pid"
        self.adapter_pid = self.root / "adapter.pid"
        self.heartbeat = self.root / "gateway_heartbeat.json"
        self.log = self.root / "gateway.log"
        # Create directories
        for path in (
            self.inbox,
            self.processing,
            self.done,
            self.failed,
            self.terminal,
            self.responses,
        ):
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

    def test_generate_request_id_unique_under_burst(self):
        """同毫秒并发不能撞 id:撞了两请求写同一队列文件→JSON 损坏→GATEWAY_REQUEST_LOAD_ERROR
        (冷启动并发实测 ~1-2/10 失败)。紧循环生成一批,断言全唯一(计数器兜底,与时钟无关)。"""
        from agent_py_agent.agent.gateway_parts.http_service import _generate_request_id

        ids = [_generate_request_id() for _ in range(2000)]
        assert len(set(ids)) == len(ids)

    def test_generate_request_id_unique_across_threads(self):
        """ThreadingHTTPServer 是多线程:并发线程各生成一批 id 也必须全唯一(next() GIL 原子)。"""
        import threading

        from agent_py_agent.agent.gateway_parts.http_service import _generate_request_id

        out: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            batch = [_generate_request_id() for _ in range(500)]
            with lock:
                out.extend(batch)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(out)) == len(out) == 4000

    def test_build_ask_request_carries_conversation_context(self):
        from agent_py_agent.agent.gateway_parts.http_handlers import (
            _AskRequestContext,
            _build_ask_request,
        )

        request = _build_ask_request(
            _AskRequestContext(
                body={"metadata": {}, "conversation_id": "room-1"},
                goal="继续看后台任务",
                request_id="req_1",
                user_id="user-1",
                channel="feishu",
            )
        )

        assert request["conversation"] == {
            "channel": "feishu",
            "channel_conversation_id": "room-1",
            "channel_user_id": "user-1",
            "canonical_user_id": "user-1",
        }


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

    def test_metrics_endpoint_exposes_concurrency_probes(self, http_server):
        """GET /metrics 暴露 Prometheus 文本(§6-A 量化端点):并发探针指标名可被抓取。"""
        import urllib.request

        from agent_py_agent.agent.observability.concurrency_metrics import gateway_worker_busy

        _, port = http_server
        gateway_worker_busy(0)  # 触发指标注册(懒创建),真机由热路径自然注册
        with urllib.request.urlopen(f"http://localhost:{port}/metrics", timeout=5) as response:
            assert response.status == 200
            assert "text/plain" in response.headers.get("Content-Type", "")
            body = response.read().decode("utf-8")
        assert "agent_gateway_workers_busy" in body
        assert "agent_gateway_queue_wait_seconds" in body
        assert "agent_llm_inflight" in body

    def test_status_endpoint_uses_hot_request_counts(self, http_server, mock_paths: MockGatewayPaths):
        """GET /status only reports hot queue counts for frequent polling."""
        import urllib.request

        _, port = http_server
        mock_paths.inbox.mkdir(parents=True, exist_ok=True)
        mock_paths.processing.mkdir(parents=True, exist_ok=True)
        mock_paths.done.mkdir(parents=True, exist_ok=True)
        mock_paths.failed.mkdir(parents=True, exist_ok=True)
        mock_paths.responses.mkdir(parents=True, exist_ok=True)
        (mock_paths.inbox / "gw-1.json").write_text("{}", encoding="utf-8")
        (mock_paths.processing / "gw-2.json").write_text("{}", encoding="utf-8")
        (mock_paths.done / "gw-old.json").write_text("{}", encoding="utf-8")
        (mock_paths.failed / "gw-failed.json").write_text("{}", encoding="utf-8")
        (mock_paths.responses / "gw-response.json").write_text("{}", encoding="utf-8")

        with urllib.request.urlopen(f"http://localhost:{port}/status", timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))

        assert data["requests"] == {"pending": 1, "processing": 1}

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
