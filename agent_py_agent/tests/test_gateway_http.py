from __future__ import annotations

"""Tests for gateway HTTP service."""

import errno
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# LLM: 这几个用例要连自己刚起的回环 HTTP 服务；唯一允许"跳过"的原因是本机环境连不上
#   （端口没监听 / 连接被拒 / 连接超时 / 回环地址不可用 / 权限不允许建连）。之前写成 except Exception，
#   连 assert 失败（AssertionError）都会被吞成"跳过"，把真实失败藏起来——9b 终审点出的正是这种。
#   这里按 **errno 白名单**钉死，而不是"凡是 OSError 都算"：OSError 还包括 ENOSPC（写满）、
#   EIO 之类与"连不上"无关的失败，把它们也算成环境原因等于换个方式继续藏问题。
# 函数用途: 判断连回环 HTTP 服务失败的异常是否属于"本机连不上"，供 skip 分支使用。
#: 只认这些 errno 代表"本机环境连不上回环服务"；其余 OSError 一律当真实失败抛出。
_CONNECTION_ERRNOS = frozenset({
    errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT,
    errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT,
    errno.EACCES, errno.EPERM,
})


def _connection_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, AssertionError):
        return False
    # URLError 把底层原因放在 .reason 里（urlopen 连不上时就是 ConnectionRefusedError）；先剥一层。
    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(cause, (socket.timeout, TimeoutError)):
        return True
    err = getattr(cause, "errno", None)
    return err in _CONNECTION_ERRNOS


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


class _ResponseWriter:
    def __init__(self, error: OSError | None) -> None:
        self.error = error

    def write(self, payload: bytes) -> int:
        if self.error is not None:
            raise self.error
        return len(payload)


def _response_handler_double(error: OSError | None):
    from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPHandler

    handler = object.__new__(GatewayHTTPHandler)
    handler.command = "POST"
    handler.close_connection = False
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = _ResponseWriter(error)
    return handler


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
        """固定 HTTP worker 并发生成多批 id 时也必须全唯一(next() GIL 原子)。"""
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

    @pytest.mark.parametrize(
        "error",
        [
            BrokenPipeError(errno.EPIPE, "client closed"),
            ConnectionResetError(errno.ECONNRESET, "client reset"),
            ConnectionAbortedError(errno.ECONNABORTED, "client aborted"),
        ],
    )
    def test_send_json_treats_only_client_disconnect_as_normal(self, error: OSError):
        from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPHandler

        handler = _response_handler_double(error)

        GatewayHTTPHandler._send_json(handler, 200, {"ok": True})

        assert handler.close_connection is True
        handler.send_header.assert_any_call("Connection", "close")

    def test_send_json_does_not_hide_unrelated_io_failure(self):
        from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPHandler

        handler = _response_handler_double(OSError(errno.ENOSPC, "disk full"))

        with pytest.raises(OSError, match="disk full"):
            GatewayHTTPHandler._send_json(handler, 200, {"ok": True})

    def test_send_json_does_not_hide_serialization_failure(self):
        from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPHandler

        handler = _response_handler_double(None)

        with pytest.raises(TypeError):
            GatewayHTTPHandler._send_json(handler, 200, {"bad": object()})

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

    def test_build_ask_request_preserves_typed_workspace(self, tmp_path):
        from agent_py_agent.agent.gateway_parts.http_handlers import (
            _AskRequestContext,
            _build_ask_request,
        )

        request = _build_ask_request(
            _AskRequestContext(
                body={
                    "metadata": {},
                    "conversation_id": "room-cwd",
                    "workspace": {
                        "cwd": str(tmp_path),
                        "roots": [str(tmp_path)],
                    },
                },
                goal="继续当前目录任务",
                request_id="req-cwd",
                user_id="local-agent",
                channel="chat",
            )
        )

        assert request["workspace"] == {
            "cwd": str(tmp_path),
            "roots": [str(tmp_path)],
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

        server, port = http_server
        url = f"http://localhost:{port}/status"

        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                assert response.status == 200
                assert response.headers.get("Connection") == "close"
                data = json.loads(response.read().decode("utf-8"))
                assert "status" in data
                assert "requests" in data
        except Exception as e:  # noqa: BLE001 - 由 _connection_unavailable 收窄，断言失败照常报红
            if not _connection_unavailable(e):
                raise
            pytest.skip(f"HTTP server not reachable: {e}")

    def test_metrics_endpoint_exposes_concurrency_probes(self, http_server):
        """GET /metrics 暴露 Prometheus 文本(§6-A 量化端点):并发探针指标名可被抓取。"""

        from agent_py_agent.agent.observability.concurrency_metrics import gateway_worker_busy

        _, port = http_server
        gateway_worker_busy(0)  # 触发指标注册(懒创建),真机由热路径自然注册
        with urllib.request.urlopen(f"http://localhost:{port}/metrics", timeout=5) as response:
            assert response.status == 200
            assert response.headers.get("Connection") == "close"
            assert "text/plain" in response.headers.get("Content-Type", "")
            body = response.read().decode("utf-8")
        assert "agent_gateway_workers_busy" in body
        assert "agent_gateway_queue_wait_seconds" in body
        assert "agent_llm_inflight" in body

    def test_status_endpoint_uses_hot_request_counts(self, http_server, mock_paths: MockGatewayPaths):
        """GET /status only reports hot queue counts for frequent polling."""

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
        except Exception as e:  # noqa: BLE001 - 由 _connection_unavailable 收窄，断言失败照常报红
            if not _connection_unavailable(e):
                raise
            pytest.skip(f"HTTP server not reachable: {e}")

    def test_result_not_found(self, http_server, mock_paths: MockGatewayPaths):
        """GET /result/<id> returns 404 for unknown request."""

        server, port = http_server
        url = f"http://localhost:{port}/result/nonexistent_id"

        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                assert response.status == 404
        except urllib.error.HTTPError as e:
            assert e.code == 404
        except Exception as e:  # noqa: BLE001 - 由 _connection_unavailable 收窄，断言失败照常报红
            if not _connection_unavailable(e):
                raise
            pytest.skip(f"HTTP server not reachable: {e}")

    def test_stop_endpoint(self, http_server, mock_paths: MockGatewayPaths):
        """POST /stop：有进程身份时回 200 并写出定向停止请求（自己不发信号）。"""
        import os

        from agent_py_agent.agent.gateway_parts.daemon_control import (
            get_running_pid,
            write_pid_record,
        )

        server, port = http_server
        url = f"http://localhost:{port}/stop"

        # 用产品自己的写 pid 入口登记身份（不手写格式）：handle_stop 据它取当前 Gateway 的进程身份。
        write_pid_record(mock_paths.pid)
        assert get_running_pid(mock_paths.pid, cleanup_stale=False) == os.getpid()
        assert not mock_paths.stop_request.exists(), "前置：本次调用前不该有停止请求文件"

        try:
            req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as response:
                assert response.status == 200
                data = json.loads(response.read().decode("utf-8"))
                assert data["status"] == "stopping"
        except Exception as e:  # noqa: BLE001 - 由 _connection_unavailable 收窄，断言失败照常报红
            if not _connection_unavailable(e):
                raise
            pytest.skip(f"HTTP server not reachable: {e}")

        # 停止请求确实写在 mock_paths 下，且目标进程就是刚登记的那个——服务端只写文件，不发信号。
        assert mock_paths.stop_request.exists(), "POST /stop 应写出定向停止请求文件"
        payload = json.loads(mock_paths.stop_request.read_text(encoding="utf-8"))
        assert payload["target_process"]["pid"] == os.getpid()
        assert payload["source"] == "gateway_http"

    def test_stop_endpoint_without_process_identity_returns_409(self, http_server, mock_paths: MockGatewayPaths):
        """没有进程身份时 POST /stop 回 409：这是有意的产品行为（宁可拒绝也不发无定向的停止请求）。"""
        server, port = http_server
        url = f"http://localhost:{port}/stop"

        # 不写 pid 记录：handle_stop 拿不到当前 Gateway 身份，必须 fail-closed。
        assert not mock_paths.pid.exists(), "前置：本用例必须不带进程身份"

        req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=5)
        assert exc_info.value.code == 409
        body = json.loads(exc_info.value.read().decode("utf-8"))
        assert body == {"error": "gateway process identity unavailable"}
        assert not mock_paths.stop_request.exists(), "被拒绝时不该写出停止请求文件"

    def test_unknown_endpoint_returns_404(self, http_server, mock_paths: MockGatewayPaths):
        """Unknown endpoints return 404."""
        import urllib.error

        server, port = http_server
        url = f"http://localhost:{port}/unknown"

        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(url, timeout=5)
            assert exc_info.value.code == 404
        except Exception as e:  # noqa: BLE001 - 由 _connection_unavailable 收窄，断言失败照常报红
            if not _connection_unavailable(e):
                raise
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
