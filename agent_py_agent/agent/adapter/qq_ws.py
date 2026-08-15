
"""QQ WebSocket client — handshake, send/receive, keepalive."""

from __future__ import annotations

import base64
import hashlib
import json
import ssl
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


def _verify_handshake(sock, key: str) -> None:
    response = b""
    while b"\r\n\r\n" not in response:
        response += sock.recv(4096)
    if b"HTTP/1.1 101" not in response and b"HTTP/1.0 101" not in response:
        sock.close()
        raise ConnectionError(f"WebSocket handshake failed: {response[:200]}")
    accept_expected = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    if accept_expected.encode() not in response:
        pass


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

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        """Establish TCP connection and complete WebSocket handshake."""
        parsed = _parse_ws_url(self.url)
        sock = _connect_socket(parsed)
        key = _send_handshake(sock, parsed, self.access_token)
        _verify_handshake(sock, key)
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

    def recv_text(self, timeout: float | None = None) -> str | None:
        """Receive a text frame, returns None on timeout."""
        if not self._connected or self._sock is None:
            return None
        self._sock.settimeout(timeout)
        try:
            data = self._sock.recv(8192)
            return self._handle_received_frame(data)
        except Exception:
            return None

    def _handle_received_frame(self, data: bytes) -> str | None:
        if not data:
            return None
        result = WebSocketFrame.parse_frame(data)
        if result is None:
            return None
        opcode, payload = result
        if opcode == WebSocketFrame.OPCODE_CLOSE:
            self._connected = False
            return None
        if opcode == WebSocketFrame.OPCODE_TEXT:
            return payload.decode("utf-8", "replace")
        if opcode == WebSocketFrame.OPCODE_PING:
            self._send_pong()
        return None

    def _send_pong(self) -> None:
        if self._sock:
            self._sock.sendall(bytes([0x8A, 0x00]))

    def close(self) -> None:
        """Gracefully close the connection."""
        self._closed = True
        if self._connected and self._sock:
            try:
                self._sock.sendall(WebSocketFrame.build_close_frame())
            except Exception:
                pass
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        self._connected = False
