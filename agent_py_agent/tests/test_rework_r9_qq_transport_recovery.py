"""R9-R1 P1-R9-20：QQ WebSocket 按连接增量缓冲完整帧；超时/EOF/连接级重连/全局停止分开；去重受理后才提交。

真实 QQAdapter / QQWebSocketClient 与本地合成网关（loopback TCP + RFC6455 帧，按用例脚本发帧）；
只替换取 token 与 gateway URL 两个 HTTP 入口和重连退避等待，不连接 QQ 或互联网。
"""
from __future__ import annotations

import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.qq import QQAdapter
from agent_py_agent.agent.adapter.qq_protocol import WebSocketFrame
from agent_py_agent.agent.adapter.qq_ws import QQWebSocketClient

TOKEN = "synthetic-token"
SWITCHING = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
PING, CLOSE = bytes([0x89, 0x00]), bytes([0x88, 0x00])
HELLO = {"op": 10, "d": {"heartbeat_interval": 60000}}
HEARTBEAT = {"op": 1, "d": None}


def _text(value: dict) -> bytes:
    return WebSocketFrame.build_text_frame(json.dumps(value).encode(), masked=False)


def _message(message_id: str, seq: int = 1) -> dict:
    return {"op": 0, "s": seq, "t": "C2C_MESSAGE_CREATE",
            "d": {"id": message_id, "content": f"synthetic {message_id}", "author": {"user_openid": "u-r9"},
                  "timestamp": "2024-01-01T00:00:00+08:00"}}


def _eventually(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


# 合成网关自带的 RFC6455 解析，不借被测 codec：返回 (opcode, payload, consumed)，帧未齐返回 None。
def _client_frame(buffer):
    if len(buffer) < 2:
        return None
    opcode, masked, length, offset = buffer[0] & 0x0F, buffer[1] & 0x80, buffer[1] & 0x7F, 2
    if length in (126, 127):
        width = 2 if length == 126 else 8
        if len(buffer) < offset + width:
            return None
        length, offset = int.from_bytes(buffer[offset:offset + width], "big"), offset + width
    key = bytes(buffer[offset:offset + 4]) if masked else b""
    offset += len(key)
    if (masked and len(key) < 4) or len(buffer) < offset + length:
        return None
    payload = bytes(buffer[offset:offset + length])
    if masked:
        payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return opcode, payload, offset + length


class _Peer:
    """合成网关侧的一条连接：发送脚本字节并解析客户端（带掩码）帧。"""

    def __init__(self, sock):
        self.sock, self.buffer = sock, bytearray()

    def send(self, *chunks, pause=0.0):
        for chunk in chunks:
            self.sock.sendall(chunk)
            time.sleep(pause)

    def read_frame(self, deadline=None):
        while (frame := _client_frame(self.buffer)) is None:
            assert deadline is None or time.monotonic() < deadline, "client kept the connection open"
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buffer += chunk
        opcode, payload, consumed = frame
        del self.buffer[:consumed]
        return opcode, payload

    def read_json(self):
        while True:
            frame = self.read_frame()
            assert frame is not None, "client closed before sending a JSON frame"
            if frame[0] == WebSocketFrame.OPCODE_TEXT:
                return json.loads(frame[1])

    def read_until_eof(self, timeout=10.0):
        deadline, frames = time.monotonic() + timeout, []
        while (frame := self.read_frame(deadline)) is not None:
            frames.append(frame)
        return frames


class _Gateway:
    """本地合成 QQ 网关：真实 loopback TCP 握手，101 响应可与首批帧同包发送。"""

    def __init__(self):
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(10)
        self.url = f"ws://127.0.0.1:{self.listener.getsockname()[1]}/websocket"
        self.socks = []

    def accept(self, after=b""):
        sock, _address = self.listener.accept()
        sock.settimeout(10)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.socks.append(sock)
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = sock.recv(4096)
            assert chunk, "client closed during handshake"
            request += chunk
        assert request.startswith(b"GET /websocket HTTP/1.1\r\n") and f"Bearer {TOKEN}".encode() in request
        sock.sendall(SWITCHING + after)
        return _Peer(sock)

    def no_more_connections(self, wait=0.5):
        self.listener.settimeout(wait)
        try:
            extra, _address = self.listener.accept()
        except TimeoutError:
            return True
        extra.close()
        return False

    def close(self):
        for sock in self.socks:
            sock.close()
        self.listener.close()


@pytest.fixture
def gateway():
    value = _Gateway()
    yield value
    value.close()


def _connected_client(gateway, after=b""):
    peers = []
    server = threading.Thread(target=lambda: peers.append(gateway.accept(after=after)))
    server.start()
    client = QQWebSocketClient(gateway.url, TOKEN)
    client.connect()
    server.join(10)
    return client, peers[0]


@pytest.fixture
def kit(gateway, monkeypatch, tmp_path):
    adapter = QQAdapter(config={"qq_app_id": "app", "qq_app_secret": "secret"}, workspace_root=tmp_path)
    monkeypatch.setattr(adapter, "_get_access_token", lambda: TOKEN)
    monkeypatch.setattr(adapter, "_fetch_gateway_url", lambda _token: gateway.url)
    real_wait, backoffs = adapter._stop_event.wait, []

    # 只缩短生命周期循环的重连退避等待，并记录原退避秒数。
    def fast_backoff(timeout=None):
        backoffs.append(timeout)
        return real_wait(0.01)

    monkeypatch.setattr(adapter._stop_event, "wait", fast_backoff)
    received = []
    adapter.on_message(lambda msg: received.append(msg.message_id))
    yield SimpleNamespace(adapter=adapter, received=received, backoffs=backoffs)
    adapter.stop()


def _identified_with_heartbeat(gateway):
    peer = gateway.accept()
    assert peer.read_json()["op"] == 2
    peer.send(_text(HELLO))
    assert peer.read_json() == HEARTBEAT
    return peer


def test_public_codec_keeps_parse_frame_and_reports_consumed_extent():
    first, second = _text({"op": 11}), _text({"op": 0, "t": "READY"})
    assert WebSocketFrame.parse_frame(first + second) == (WebSocketFrame.OPCODE_TEXT, b'{"op": 11}')
    assert WebSocketFrame.parse_frame_extent(first + second) == (
        WebSocketFrame.OPCODE_TEXT, b'{"op": 11}', len(first))
    assert WebSocketFrame.parse_frame_extent(bytearray(first[:-1])) is None


def test_client_buffers_post_handshake_split_and_coalesced_frames(gateway):
    split = _message("m-split")
    split["d"]["content"] = "x" * 300  # 16 位长度头也被拆开
    first, second = _message("m-first", 2), _message("m-second", 3)
    frame, observed = _text(split), {}
    client, peer = _connected_client(gateway, after=_text(HELLO))  # Hello 与 101 同包到达

    def serve():
        peer.send(frame[:1], frame[1:3], frame[3:40], frame[40:], pause=0.05)
        peer.send(_text(first) + PING + _text(second))  # 两帧夹 ping 合并为一次发送
        observed["pong"] = peer.read_frame()

    server = threading.Thread(target=serve)
    server.start()
    texts, deadline = [], time.monotonic() + 10
    while len(texts) < 4 and time.monotonic() < deadline:
        text = client.recv_text(timeout=1.0)
        if text is not None:
            texts.append(json.loads(text))
    server.join(10)
    client.close()
    assert texts == [HELLO, split, first, second]
    assert observed["pong"] == (WebSocketFrame.OPCODE_PONG, b"")


def test_eof_and_close_frame_end_connection_instead_of_reporting_timeouts(gateway):
    client, peer = _connected_client(gateway)
    assert client.recv_text(timeout=0.2) is None  # 真超时仍是 None
    peer.sock.shutdown(socket.SHUT_WR)
    with pytest.raises(ConnectionError):
        client.recv_text(timeout=1.0)
    assert client.is_connected is False
    with pytest.raises(ConnectionError):
        client.recv_text(timeout=1.0)
    client.close()
    client, _peer = _connected_client(gateway, after=CLOSE)
    with pytest.raises(ConnectionError):
        client.recv_text(timeout=1.0)
    assert client.is_connected is False
    client.close()


def test_opcode7_reconnects_connection_locally_without_global_stop(gateway, kit):
    adapter = kit.adapter
    adapter.start()
    first = _identified_with_heartbeat(gateway)
    first_ws, first_heartbeat = adapter._ws, adapter._heartbeat_thread
    first.send(_text({"op": 7, "d": None}))
    assert first.read_until_eof() == [(WebSocketFrame.OPCODE_CLOSE, b"")]
    second = gateway.accept()
    assert second.read_json()["op"] == 2
    second.send(_text(_message("m-after-op7")))
    assert _eventually(lambda: kit.received == ["m-after-op7"])
    # 只结束旧连接：旧心跳已 join、新连接接替；全局停止未设，按未失败退避一次后重连。
    assert not first_heartbeat.is_alive() and first_ws.is_connected is False and adapter._ws is not first_ws
    assert not adapter._stop_event.is_set() and adapter.running and kit.backoffs == [30.0]


def test_heartbeat_failure_reconnects_only_that_connection(gateway, kit, monkeypatch):
    failed, real_send_json = [], QQWebSocketClient.send_json

    def send_json(self, data):
        if data.get("op") == 1 and not failed:
            failed.append(self)
            raise OSError("synthetic heartbeat failure")
        return real_send_json(self, data)

    monkeypatch.setattr(QQWebSocketClient, "send_json", send_json)
    adapter = kit.adapter
    adapter.start()
    first = gateway.accept()
    assert first.read_json()["op"] == 2
    first.send(_text(HELLO))
    assert first.read_until_eof() == [(WebSocketFrame.OPCODE_CLOSE, b"")]
    second = _identified_with_heartbeat(gateway)
    assert len(failed) == 1 and failed[0].is_connected is False and adapter._ws not in {None, failed[0]}
    assert not adapter._stop_event.is_set() and adapter.running
    second.send(_text(_message("m-after-heartbeat")))
    assert _eventually(lambda: kit.received == ["m-after-heartbeat"])


@pytest.mark.parametrize("failure", ["refused", "error"])
def test_dedupe_commits_only_after_accepted_callback(tmp_path, failure):
    adapter = QQAdapter(config={"qq_app_id": "", "qq_app_secret": ""}, workspace_root=tmp_path)
    calls = []

    def callback(msg):
        calls.append(msg.message_id)
        if len(calls) == 1:
            if failure == "error":
                raise RuntimeError("synthetic downstream error")
            return False
        return None  # legacy None 视为受理

    adapter.on_message(callback)
    payload = _message("m-retry")["d"]
    adapter._process_qq_message(payload)
    assert "m-retry" not in adapter._seen_ids and not adapter._inflight_ids
    adapter._process_qq_message(payload)  # 平台重投：未受理的消息再次交付并被受理
    adapter._process_qq_message(payload)  # 受理后的重复投递才被去重
    assert calls == ["m-retry", "m-retry"] and adapter._seen_ids == {"m-retry"} and not adapter._inflight_ids


def test_inflight_guard_blocks_concurrent_duplicate_and_releases_after_refusal(tmp_path):
    adapter = QQAdapter(config={"qq_app_id": "", "qq_app_secret": ""}, workspace_root=tmp_path)
    entered, release, calls = threading.Event(), threading.Event(), []

    def callback(msg):
        calls.append(msg.message_id)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            return False
        return True

    adapter.on_message(callback)
    payload = _message("m-inflight")["d"]
    worker = threading.Thread(target=adapter._process_qq_message, args=(payload,))
    worker.start()
    assert entered.wait(5)
    # 回调在锁外运行；在途期间的并发重复投递不再二次交付。
    assert adapter._seen_lock.acquire(timeout=1)
    adapter._seen_lock.release()
    adapter._process_qq_message(payload)
    assert calls == ["m-inflight"] and adapter._inflight_ids == {"m-inflight"}
    release.set()
    worker.join(5)
    assert not worker.is_alive() and not adapter._inflight_ids and "m-inflight" not in adapter._seen_ids
    adapter._process_qq_message(payload)
    assert calls == ["m-inflight", "m-inflight"] and adapter._seen_ids == {"m-inflight"}


def test_explicit_stop_closes_exact_socket_and_joins_threads_without_reconnect(gateway, kit):
    adapter = kit.adapter
    adapter.start()
    peer = _identified_with_heartbeat(gateway)
    ws, ws_thread, heartbeat = adapter._ws, adapter._ws_thread, adapter._heartbeat_thread
    started = time.monotonic()
    adapter.stop()
    # shutdown 唤醒阻塞接收，停止不必等满接收超时；两类线程均已 join。
    assert time.monotonic() - started < 3
    assert not ws_thread.is_alive() and not heartbeat.is_alive()
    assert adapter._ws is None and adapter._ws_thread is None and adapter._heartbeat_thread is None
    assert ws.is_connected is False and adapter.running is False
    assert peer.read_until_eof() == [(WebSocketFrame.OPCODE_CLOSE, b"")]
    assert gateway.no_more_connections() and kit.backoffs == []


def test_stop_from_message_callback_never_joins_current_thread(gateway, kit):
    adapter, outcome = kit.adapter, []

    def callback(msg):
        try:
            adapter.stop()
            outcome.append("stopped")
        except BaseException as exc:  # noqa: BLE001 - 记录原样异常供断言
            outcome.append(exc)
        return True

    adapter.on_message(callback)
    adapter.start()
    peer = _identified_with_heartbeat(gateway)
    ws_thread, heartbeat = adapter._ws_thread, adapter._heartbeat_thread
    peer.send(_text(_message("m-stop")))
    assert _eventually(lambda: outcome)
    assert outcome == ["stopped"]
    ws_thread.join(5)
    assert not ws_thread.is_alive() and not heartbeat.is_alive() and adapter.running is False
    assert peer.read_until_eof() == [(WebSocketFrame.OPCODE_CLOSE, b"")]
    assert gateway.no_more_connections() and "m-stop" in adapter._seen_ids


# 放在最后：未修复实现会在握手 EOF 上空转。
def test_handshake_eof_fails_instead_of_spinning(gateway):
    outcome = []

    def connect():
        try:
            QQWebSocketClient(gateway.url, TOKEN).connect()
            outcome.append("connected")
        except ConnectionError as exc:
            outcome.append(exc)

    worker = threading.Thread(target=connect, daemon=True)
    worker.start()
    sock, _address = gateway.listener.accept()
    sock.settimeout(10)
    assert sock.recv(4096).startswith(b"GET /websocket HTTP/1.1")
    sock.close()
    worker.join(5)
    assert not worker.is_alive() and len(outcome) == 1 and isinstance(outcome[0], ConnectionError)
