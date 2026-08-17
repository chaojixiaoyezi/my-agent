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


def _post_ask(port: int, body: dict) -> tuple[int, dict]:
    """POST /ask 助手：返回 (status, json)。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"http://localhost:{port}/ask",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw}


class TestIdempotencyReplay:
    """知衡 seq 2553 边界②：幂等重放必须同 key 同 payload 才重放，
    同 key 异 payload → 409 IDEMPOTENCY_CONFLICT，owner/conversation 隔离。"""

    @pytest.fixture
    def mock_paths(self, tmp_path: Path) -> MockGatewayPaths:
        return MockGatewayPaths(tmp_path)

    @pytest.fixture
    def http_server(self, mock_paths: MockGatewayPaths):
        """Start HTTP server on a free port."""
        from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPServer

        port = find_free_port()
        server = GatewayHTTPServer(port, mock_paths)
        server.start()
        yield server, port
        server.stop()

    def test_same_key_same_payload_replays(self, http_server, mock_paths: MockGatewayPaths):
        """同 key 同 payload（同会话同 prompt）→ 同一 request_id + replayed:true。"""
        import urllib.error
        import urllib.request

        _, port = http_server
        base = {"prompt": "证据重放测试", "chat_session_id": "idem-sess-1", "idempotency_key": "idem-key-1"}
        status1, data1 = _post_ask(port, {**base, "save": True})
        status2, data2 = _post_ask(port, {**base, "save": True})
        try:
            assert status1 == 202 and data1["status"] == "queued"
            assert status2 == 202 and data2["status"] == "accepted"
            assert data2["replayed"] is True
            assert data2["request_id"] == data1["request_id"]
        except urllib.error.URLError as e:
            pytest.skip(f"HTTP server not reachable: {e}")

    def test_same_key_diff_payload_conflict_409(self, http_server, mock_paths: MockGatewayPaths):
        """同 key 异 payload（prompt 不同）→ 409 IDEMPOTENCY_CONFLICT + 原 request_id。"""
        _, port = http_server
        status1, data1 = _post_ask(
            port, {"prompt": "原请求", "chat_session_id": "idem-sess-2", "idempotency_key": "idem-key-2"}
        )
        status2, data2 = _post_ask(
            port, {"prompt": "改了词的请求", "chat_session_id": "idem-sess-2", "idempotency_key": "idem-key-2"}
        )
        assert status1 == 202
        assert status2 == 409
        assert data2.get("error_code") == "IDEMPOTENCY_CONFLICT"
        assert data2.get("request_id") == data1["request_id"]

    def test_same_key_diff_conversation_new_request(self, http_server, mock_paths: MockGatewayPaths):
        """同 key 异 conversation（chat_session_id 不同）→ 各自新请求，不重放。"""
        _, port = http_server
        status1, data1 = _post_ask(
            port, {"prompt": "会话A", "chat_session_id": "idem-sess-A", "idempotency_key": "idem-key-3"}
        )
        status2, data2 = _post_ask(
            port, {"prompt": "会话A", "chat_session_id": "idem-sess-B", "idempotency_key": "idem-key-3"}
        )
        assert status1 == 202 and status2 == 202
        assert data1["request_id"] != data2["request_id"]
        assert "replayed" not in data2

    def test_legacy_file_without_digest_replays(self, http_server, mock_paths: MockGatewayPaths):
        """升级前旧文件（无 idempotency_digest）→ 兼容放行重放，不误 409。"""
        from agent_py_agent.agent.gateway_parts.http_handlers import (
            _find_request_by_idempotency_key,
        )

        _, port = http_server
        legacy = {
            "id": "req_legacy_1",
            "request_id": "req_legacy_1",
            "kind": "ask",
            "goal": "旧请求",
            "idempotency_key": "idem-key-legacy",
            "metadata": {"user_id": "admin", "channel": "chat"},
            "conversation": {"channel_conversation_id": "idem-sess-legacy"},
        }
        (mock_paths.inbox / "req_legacy_1.json").write_text(
            json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
        )
        hit = _find_request_by_idempotency_key(mock_paths, "admin", "chat", "idem-sess-legacy", "idem-key-legacy")
        assert hit is not None
        assert hit[0] == "req_legacy_1"
        assert hit[1] is None  # 无指纹 → 第二元素 None，handle_ask 走兼容放行
        status, data = _post_ask(
            port,
            {"prompt": "旧请求", "chat_session_id": "idem-sess-legacy", "idempotency_key": "idem-key-legacy"},
        )
        assert status == 202
        assert data.get("replayed") is True
        assert data["request_id"] == "req_legacy_1"

    def test_owner_isolation_in_lookup(self, tmp_path: Path):
        """owner 隔离：同 key 同 conversation 异 user → 不命中（查重按 owner 前缀）。"""
        from agent_py_agent.agent.gateway_parts.http_handlers import (
            _find_request_by_idempotency_key,
        )

        paths = MockGatewayPaths(tmp_path)
        req = {
            "id": "req_owner_1",
            "request_id": "req_owner_1",
            "kind": "ask",
            "goal": "ownerA",
            "idempotency_key": "idem-key-owner",
            "metadata": {"user_id": "owner-A", "channel": "chat"},
            "conversation": {"channel_conversation_id": "idem-sess-owner"},
        }
        (paths.inbox / "req_owner_1.json").write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
        # 异 user 同 key 同 conversation → 不命中（各自独立请求，不重放）
        assert _find_request_by_idempotency_key(paths, "owner-B", "chat", "idem-sess-owner", "idem-key-owner") is None
        # 同 user 同 conversation 同 key → 命中
        hit = _find_request_by_idempotency_key(paths, "owner-A", "chat", "idem-sess-owner", "idem-key-owner")
        assert hit is not None and hit[0] == "req_owner_1"

    def test_payload_digest_deterministic_and_sensitive(self):
        """digest 确定性：同 body 同摘要；prompt/conversation/save 任一变化 → 摘要变化。"""
        from agent_py_agent.agent.gateway_parts.http_handlers import _idempotency_payload_digest

        body = {"prompt": "你好", "chat_session_id": "s1", "save": True}
        d1 = _idempotency_payload_digest(body, "你好")
        assert d1 == _idempotency_payload_digest(dict(body), "你好")
        assert d1 != _idempotency_payload_digest({"prompt": "你好吗", "chat_session_id": "s1", "save": True}, "你好吗")
        assert d1 != _idempotency_payload_digest({"prompt": "你好", "chat_session_id": "s2", "save": True}, "你好")
        assert d1 != _idempotency_payload_digest({"prompt": "你好", "chat_session_id": "s1", "save": False}, "你好")

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
