"""Focused network transport tests, including real loopback SSE reconnects (not external reachability)."""
from __future__ import annotations

import builtins
import io
import json
import threading
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.business_stream_pool import BusinessStreamPool
from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPServer
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path, gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.progress_text_delta import TextDeltaBuffer
from agent_py_agent.agent.gateway_parts.queue_service import archive_request
from agent_py_agent.agent.gateway_parts.request_event_hub import RequestEventHub, bind_hub
from agent_py_agent.agent.gateway_parts.request_event_routes import encode_sse
from agent_py_agent.cli.chat_parts.gateway_client import (
    GatewayChunkPollRequest,
    NetworkGatewayObserver,
    network_gateway_observer,
)


# LLM: HTTP tests own disposable queue roots/listeners and close their latest restarted instance; no daemon outside this test is modified.
# 函数用途: 准备真实本机HTTP入口，允许在同路径同端口重建监听器和中心。
@pytest.fixture
def live_gateway(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses, paths.terminal):
        folder.mkdir(parents=True, exist_ok=True)
    state = {"paths": paths}
    def start(*, ring=8, port=0, streams=2):
        server = GatewayHTTPServer(port, paths)
        server.request_event_hub = RequestEventHub(ring_events=ring, topic_capacity=32, subscribers_per_topic=8,
            idle_seconds=1, fallback_poll_seconds=0.05)
        bind_hub(paths, server.request_event_hub)
        server.start()
        pool = BusinessStreamPool(max_streams=streams, per_owner_max=streams, write_timeout_seconds=0.2)
        pool.start()
        server.business_stream_pool = pool
        state.update(server=server, pool=pool, port=server.server.server_address[1])
        return server
    state["start"] = start
    yield state
    if "server" in state:
        state["server"].stop()


# LLM: Generate genuine committed text-delta events through the production buffering/redaction layer, not hand-made transport-only fragments.
# 函数用途: 生成超过2万字的真实TextDeltaBuffer事件和准确原文。
def text_events():
    text = ("山河远阔烟火人间。" * 3000)[:20000]
    buffer = TextDeltaBuffer(holdback_chars=32, flush_chars=32)
    events = buffer.begin("model-call")
    for offset in range(0, len(text), 160):
        events.extend(buffer.append(text[offset:offset + 160], model_call_id="model-call"))
    events.extend(buffer.finish("model-call", text=text))
    assert "".join(str(e.get("text", "")) for e in events if e["kind"] == "assistant_text_delta") == text
    return text, events


# LLM: Restrict filesystem assertions to the actual client thread; server threads must still read their authoritative files.
# 函数用途: 禁止网络客户端读取服务器目录或G1文件，同时允许真实服务端读取队列。
def prohibit_client_files(monkeypatch, root):
    client = threading.get_ident()
    original = Path.open
    observed = []
    def guarded(path, *args, **kwargs):
        if threading.get_ident() == client and (path.is_relative_to(root) or "credential" in path.name):
            observed.append(str(path))
            raise AssertionError("network client attempted a server/G1 file read")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded)
    original_open = builtins.open
    def guarded_open(file, *args, **kwargs):
        if isinstance(file, (str, Path)):
            path = Path(file)
            if threading.get_ident() == client and (path.is_relative_to(root) or "credential" in path.name):
                observed.append(str(path))
                raise AssertionError("network client attempted a server/G1 open")
        return original_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", guarded_open)
    return observed


# LLM: Resume uses a durable row id after a genuine closed HTTP response. Ring overflow and listener/hub restart cannot select an offset.
# 函数用途: 通过真实HTTP断连/重连验证2万字无漏无重，并在客户端线程禁止任何本地服务器/G1读取。
@pytest.mark.parametrize("ring", [8, 1024])
@pytest.mark.parametrize("restart", [False, True])
def test_real_sse_twenty_thousand_chars_reconnect(live_gateway, monkeypatch, ring, restart):
    state = live_gateway
    paths = state["paths"]
    text, events = text_events()
    record = paths.processing / "r.json"
    record.write_text(json.dumps({"id": "r", "execution_attempt_id": "attempt-a", "user_id": "alice", "channel": "chat"}))
    gateway_chunk_path(paths, "r").write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events))
    archived = archive_request(paths, record, paths.done, "r", terminal_response={"ok": True, "response": text,
        "id": "r", "request_id": "r", "user_id": "alice", "channel": "chat", "execution_attempt_id": "attempt-a"})
    assert archived.disposition.value in {"committed", "already_committed"}, archived
    server = state["start"](ring=ring)
    for _ in range(ring + 9):
        server.request_event_hub.publish(("request", "r"), "chunk", "r", {})
    import agent_py_agent.agent.common.authenticated_http as transport
    actual_open = transport.open_authenticated_request
    opened, consumed = [], []
    class Disconnect:
        def __init__(self, raw):
            self.raw, self.frames = raw, 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.raw.close()
        def readline(self, size):
            if self.frames >= 12:
                self.raw.close()
                if restart:
                    port = state["port"]
                    state["server"].stop()
                    state["start"](ring=ring, port=port)
                    assert state["server"].request_event_hub.publish(("request", "r"), "chunk", "r", {}) == 1
                return b""
            line = self.raw.readline(size)
            if line in (b"\n", b"\r\n"):
                self.frames += 1
            return line
    def opener(request, *, timeout):
        opened.append(request)
        raw = actual_open(request, timeout=timeout)
        return Disconnect(raw) if len(opened) == 1 else raw
    monkeypatch.setattr(transport, "open_authenticated_request", opener)
    agent = SimpleNamespace(config=SimpleNamespace(gateway_port=state["port"], gateway_request_timeout=15, gateway_auth_token=""))
    observer = NetworkGatewayObserver(agent, f"http://127.0.0.1:{state['port']}")
    def consume(event):
        consumed.append(event)
        return True
    request = GatewayChunkPollRequest(gateway_chunk_path(paths, "r"), paths.terminal / "r.json", time.time() + 15,
        lambda text: None, [0], [0], on_event=consume)
    observed = prohibit_client_files(monkeypatch, paths.root)
    result = observer(request)
    assert result["ok"] is True
    assert result["response"] == text
    assert len(opened) == 2
    resumed = opened[1].get_header("Last-event-id")
    assert resumed and resumed.startswith("v1:r:attempt-a:")
    assert "".join(str(e.get("text", "")) for e in consumed if e["kind"] == "assistant_text_delta") == text
    sequences = [e["seq"] for e in consumed if e["kind"] == "assistant_text_delta"]
    assert sequences == list(range(len(sequences)))
    assert not observed


# LLM: Incomplete UTF-8 and blank-line-less frames must have no cursor effect across reconnect.
# 函数用途: 对半帧注入断连，核对恢复请求不带未消费的游标。
def test_half_utf8_frame_does_not_advance(monkeypatch, tmp_path):
    import agent_py_agent.agent.common.authenticated_http as transport
    first = encode_sse(("progress", {"event": {"kind": "assistant_commentary", "text": "中文"}}, "v1:r:a:1:99"))
    partial = first[:first.index("中".encode()) + 1]
    terminal = encode_sse(("terminal", {"result": {"ok": True, "response": "done"}}, "v1:r:a:1:99"))
    replies = [io.BytesIO(partial), io.BytesIO(first + terminal)]
    headers, events = [], []
    def opener(request, *, timeout):
        headers.append(dict(request.header_items()))
        return replies.pop(0)
    monkeypatch.setattr(transport, "open_authenticated_request", opener)
    observer = NetworkGatewayObserver(SimpleNamespace(config=SimpleNamespace(gateway_request_timeout=3)), "http://example.invalid")
    result = observer(GatewayChunkPollRequest(tmp_path / "r.chunks.jsonl", tmp_path / "r.json", time.time()+3,
        lambda t: None, [0], on_event=lambda e: events.append(e)))
    assert result["response"] == "done"
    assert len(events) == 1
    assert all("Last-event-id" not in value for value in headers)


# LLM: Busy/failed authentication/repeated connection failures must permanently choose HTTP for this request, not server-file polling.
# 函数用途: 分别反证503、刷新失败与重复断连后的本地文件降级。
@pytest.mark.parametrize("failure", ["busy", "auth", "connect"])
def test_http_only_fallback_for_rest_of_request(monkeypatch, tmp_path, failure):
    import agent_py_agent.agent.common.authenticated_http as transport
    calls, polled = [], []
    def opener(request, *, timeout):
        calls.append(request)
        if failure == "connect":
            raise OSError("disconnected")
        raise urllib.error.HTTPError(request.full_url, 503 if failure == "busy" else 401, "denied", {}, io.BytesIO())
    monkeypatch.setattr(transport, "open_authenticated_request", opener)
    agent = SimpleNamespace(config=SimpleNamespace(gateway_request_timeout=3), refresh_gateway_network_credentials=lambda: False)
    observer = NetworkGatewayObserver(agent, "http://example.invalid")
    def request_json(method, path, payload=None, **kwargs):
        polled.append(path)
        return (200, {"events": [], "next": 0}) if path.startswith("/progress/") else (200, {"ok": True, "response": "result"})
    monkeypatch.setattr(observer, "request_json", request_json)
    observed = prohibit_client_files(monkeypatch, tmp_path)
    result = observer(GatewayChunkPollRequest(tmp_path/"r.chunks.jsonl", tmp_path/"r.json", time.time()+3, lambda _: None, [0]))
    assert result["response"] == "result"
    assert observer._http_only
    assert len(calls) == (2 if failure == "connect" else 1)
    assert len(polled) == 2
    assert not observed


# LLM: Explicit network mode does not depend on loopback reachability or local file existence.
# 函数用途: 验证远端地址自动选HTTP，本机默认不改，强制开关生效。
def test_network_mode_selection(monkeypatch):
    agent = SimpleNamespace(config=SimpleNamespace(gateway_port=42))
    monkeypatch.delenv("MY_AGENT_TUI_NETWORK", raising=False)
    monkeypatch.delenv("MY_AGENT_GATEWAY_URL", raising=False)
    assert network_gateway_observer(agent) is None
    agent.gateway_url = "https://remote.example"
    assert isinstance(network_gateway_observer(agent), NetworkGatewayObserver)
    agent.gateway_url = "http://localhost:42"
    monkeypatch.setenv("MY_AGENT_TUI_NETWORK", "1")
    assert isinstance(network_gateway_observer(agent), NetworkGatewayObserver)
