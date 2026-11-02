
"""QQ WebSocket client — handshake, send/receive, keepalive."""

from __future__ import annotations

import base64
import hashlib
import json
import ssl
import threading
from socket import SHUT_RDWR
from socket import socket as _socket
from typing import Any
from urllib.parse import urlsplit

from .qq_protocol import WebSocketFrame

__all__ = ["QQWebSocketClient"]


def _parse_ws_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in {"ws", "wss"}:
        raise ValueError(f"Unknown WebSocket URL scheme: {url}")
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    request_target = f"{path}?{parsed.query}" if parsed.query else path
    return parsed.scheme == "wss", parsed.hostname or "", port, request_target


def _connect_socket(parsed):
    is_ssl, host, port, _request_target = parsed
    sock = _socket()
    sock.settimeout(30)
    if is_ssl:
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)
    sock.connect((host, port))
    return sock


def _send_handshake(sock, parsed, access_token: str) -> str:
    import secrets

    _is_ssl, host, port, request_target = parsed
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    handshake = (
        f"GET {request_target} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"Authorization: Bearer {access_token}\r\n"
        f"\r\n"
    )
    sock.sendall(handshake.encode())
    return key


# LLM: 只读到首个头部终止符；与 101 同包到达的首批帧原样交回连接缓冲，握手期 EOF 失败而不空转。
# 函数用途: 校验握手响应并返回头部之后已收到的帧字节。
def _verify_handshake(sock, key: str) -> bytes:
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise ConnectionError("WebSocket handshake closed before response")
        response += chunk
    head, _separator, remainder = response.partition(b"\r\n\r\n")
    if b"HTTP/1.1 101" not in head and b"HTTP/1.0 101" not in head:
        sock.close()
        raise ConnectionError(f"WebSocket handshake failed: {response[:200]}")
    accept_expected = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    if accept_expected.encode() not in response:
        pass
    return remainder


class QQWebSocketClient:
    """QQ WebSocket client — handles handshake, messaging, and keepalive."""

    def __init__(self, url: str, access_token: str, intents: int = 1 << 30) -> None:
        self.url = url
        self.access_token = access_token
        self.intents = intents
        self._sock: _socket | None = None
        self._connected = False
        self._closed = False
        self._last_seq = 0
        self._buffer = bytearray()
        self._interrupt_lock = threading.Lock()
        self._interrupted = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        """Establish TCP connection and complete WebSocket handshake."""
        parsed = _parse_ws_url(self.url)
        sock = _connect_socket(parsed)
        key = _send_handshake(sock, parsed, self.access_token)
        self._buffer = bytearray(_verify_handshake(sock, key))
        self._sock = sock
        self._connected = True

    def send_text(self, payload: str | bytes) -> None:
        """Send a text frame."""
        if not self._connected or self._sock is None:
            raise ConnectionError("Not connected")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        frame = WebSocketFrame.build_text_frame(payload, masked=True)
        self._sock.sendall(frame)

    def send_json(self, data: dict[str, Any]) -> None:
        """Send a JSON text frame."""
        self.send_text(json.dumps(data, ensure_ascii=False))

    # LLM: 每连接一份增量缓冲：先交付已缓冲的完整帧（含握手后同包字节），半帧保留待补读，多帧逐个消费；
    #   超时或帧未齐返回 None；EOF、close 帧与 socket 错误标记断开并抛 ConnectionError，不再空转或丢帧。
    # 函数用途: 返回下一条完整文本帧，途中自动应答 ping。
    def recv_text(self, timeout: float | None = None) -> str | None:
        """Receive a text frame; None on timeout or while a frame is incomplete."""
        sock = self._sock
        if not self._connected or sock is None:
            raise ConnectionError("QQ WebSocket is not connected")
        frame = WebSocketFrame.parse_frame_extent(self._buffer)
        if frame is None:
            try:
                sock.settimeout(timeout)
                data = sock.recv(8192)
            except TimeoutError:
                return None
            except OSError as exc:
                self._connected = False
                raise ConnectionError("QQ WebSocket receive failed") from exc
            if not data:
                self._connected = False
                raise ConnectionError("QQ WebSocket closed by peer")
            self._buffer += data
            frame = WebSocketFrame.parse_frame_extent(self._buffer)
            if frame is None:
                return None
        opcode, payload, consumed = frame
        del self._buffer[:consumed]
        return self._handle_received_frame(opcode, payload)

    def _handle_received_frame(self, opcode: int, payload: bytes) -> str | None:
        if opcode == WebSocketFrame.OPCODE_CLOSE:
            self._connected = False
            raise ConnectionError("QQ WebSocket closed by server")
        if opcode == WebSocketFrame.OPCODE_TEXT:
            return payload.decode("utf-8", "replace")
        if opcode == WebSocketFrame.OPCODE_PING:
            self._send_pong()
        return None

    def _send_pong(self) -> None:
        if self._sock:
            self._sock.sendall(bytes([0x8A, 0x00]))

    # LLM: 他线程只终止本连接并唤醒阻塞 recv（close 帧 + shutdown），不释放描述符：跨线程 close 会让阻塞中的
    #   poll 丢失唤醒而白等接收超时；描述符由接收线程收尾 close()。多次/并发调用只发一次 close 帧，尽力不抛。
    def interrupt(self) -> None:
        """Terminate the connection and wake a blocked receive; the receiving thread releases the socket."""
        sock = self._sock
        with self._interrupt_lock:
            if sock is None or self._interrupted:
                return
            self._interrupted = True
        if self._connected:
            try:
                sock.sendall(WebSocketFrame.build_close_frame())
            except Exception:
                pass
        try:
            sock.shutdown(SHUT_RDWR)
        except OSError:
            pass
        self._connected = False

    def close(self) -> None:
        """Gracefully close the connection."""
        self.interrupt()
        self._closed = True
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        self._connected = False
