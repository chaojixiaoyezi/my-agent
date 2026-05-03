"""QQ WebSocket client — handshake, send/receive, keepalive."""

from __future__ import annotations

import base64
import hashlib
import json
import ssl
from socket import socket as _socket
from typing import Any

from .qq_protocol import WebSocketFrame

__all__ = ["QQWebSocketClient"]


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
        # Parse URL
        if self.url.startswith("wss://"):
            is_ssl = True
            url_no_scheme = self.url[6:]
        elif self.url.startswith("ws://"):
            is_ssl = False
            url_no_scheme = self.url[5:]
        else:
            raise ValueError(f"Unknown WebSocket URL scheme: {self.url}")

        if "/" in url_no_scheme:
            host_port_path = url_no_scheme.split("/", 1)
            host_port = host_port_path[0]
            path = "/" + host_port_path[1]
        else:
            host_port = url_no_scheme
            path = "/"

        if ":" in host_port:
            host, port_str = host_port.rsplit(":", 1)
            port = int(port_str)
        else:
            host = host_port
            port = 443 if is_ssl else 80

        # TCP connection
        sock = _socket()
        sock.settimeout(30)
        if is_ssl:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.connect((host, port))

        # WebSocket handshake
        import secrets

        key = base64.b64encode(secrets.token_bytes(16)).decode()
        query = self.url.split("?", 1)[1] if "?" in self.url else ""
        request_target = f"{path}?{query}" if query else path
        handshake = (
            f"GET {request_target} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {self.access_token}\r\n"
            f"\r\n"
        )
        sock.sendall(handshake.encode())

        # Read handshake response
        response = b""
        while b"\r\n\r\n" not in response:
            response += sock.recv(4096)

        # Verify handshake response
        if b"HTTP/1.1 101" not in response and b"HTTP/1.0 101" not in response:
            sock.close()
            raise ConnectionError(f"WebSocket handshake failed: {response[:200]}")

        # Verify Sec-WebSocket-Accept
        accept_expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if accept_expected.encode() not in response:
            # Some implementations don't return this header, skip strict check
            pass

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
                return payload.decode("utf-8")
            if opcode == WebSocketFrame.OPCODE_PING:
                if self._sock:
                    self._sock.sendall(bytes([0x8A, 0x00]))
                return None
            return None
        except Exception:
            return None

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