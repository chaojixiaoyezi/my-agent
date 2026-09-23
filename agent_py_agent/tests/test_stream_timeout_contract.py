"""流式模型首事件与滚动空闲合同的定向验收。"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.backends.gateway_helpers import GatewayRequest, post_stream_iter


class _SSEHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if self.path == "/healthy-long":
            for index in range(6):
                self.wfile.write(f'data: {{"n":{index}}}\n\n'.encode())
                self.wfile.flush()
                time.sleep(0.2)
            return
        if self.path == "/slow-first":
            time.sleep(1.3)
            self.wfile.write(b'data: {"n":1}\n\n')
            self.wfile.flush()
            return
        if self.path == "/first-then-idle":
            self.wfile.write(b'data: {"n":1}\n\n')
            self.wfile.flush()
            time.sleep(1.3)
            return
        if self.path == "/comments-only":
            for _ in range(10):
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(0.1)
            return

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture(scope="module")
def sse_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _request(
    server: ThreadingHTTPServer,
    path: str,
    *,
    idle: float,
    first_event: float | None = None,
) -> GatewayRequest:
    return GatewayRequest(
        api_base=f"http://127.0.0.1:{server.server_address[1]}",
        api_key="test-key",
        path=path,
        payload={"model": "slow-model"},
        headers={},
        timeout=idle,
        first_event_timeout=first_event,
    )


def test_valid_data_can_outlive_old_total_wall(sse_server: ThreadingHTTPServer) -> None:
    started = time.monotonic()
    # 首事件含本地服务器调度/连接；本用例只锁定有效数据之后的短 idle 与长总流。
    lines = list(post_stream_iter(_request(sse_server, "/healthy-long", idle=0.4, first_event=10.0)))
    elapsed = time.monotonic() - started
    assert len(lines) == 6
    assert elapsed > 0.9


def test_request_local_first_event_budget_exceeds_idle(sse_server: ThreadingHTTPServer) -> None:
    lines = list(
        post_stream_iter(
            _request(sse_server, "/slow-first", idle=0.25, first_event=10.0)
        )
    )
    assert len(lines) == 1


def test_first_event_timeout_is_typed(sse_server: ThreadingHTTPServer) -> None:
    with pytest.raises(ProviderTimeoutError) as error:
        list(post_stream_iter(_request(sse_server, "/slow-first", idle=0.25)))
    assert error.value.stage == "first_event"


def test_idle_after_first_event_is_typed(sse_server: ThreadingHTTPServer) -> None:
    with pytest.raises(ProviderTimeoutError) as error:
        list(post_stream_iter(_request(sse_server, "/first-then-idle", idle=0.25, first_event=10.0)))
    assert error.value.stage == "stream_idle"


def test_sse_comments_do_not_fake_model_progress(sse_server: ThreadingHTTPServer) -> None:
    with pytest.raises(ProviderTimeoutError) as error:
        list(post_stream_iter(_request(sse_server, "/comments-only", idle=0.25)))
    assert error.value.stage == "first_event"
