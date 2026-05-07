# LLM: External adapter module; keep platform payload and runtime boundary contracts stable.
# 模块用途: 对接 QQ、飞书等外部渠道，把平台事件转换成内部请求。

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


# LLM: _parse_ws_url 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 解析并归一化wsurl的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _parse_ws_url(url: str):
    parsed = urlsplit(url)
    if parsed.scheme not in {"ws", "wss"}:
        raise ValueError(f"Unknown WebSocket URL scheme: {url}")
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    path = parsed.path or "/"
    request_target = f"{path}?{parsed.query}" if parsed.query else path
    return parsed.scheme == "wss", parsed.hostname or "", port, request_target


# LLM: _connect_socket 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理connectsocket相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
def _connect_socket(parsed):
    is_ssl, host, port, _request_target = parsed
    sock = _socket()
    sock.settimeout(30)
    if is_ssl:
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)
    sock.connect((host, port))
    return sock


# LLM: _send_handshake 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 发送handshake请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
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


# LLM: _verify_handshake 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理verifyhandshake相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
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


# LLM: QQWebSocketClient 属于外部通道适配的类边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 类用途: 封装qqwebsocketclient相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发通道配置、消息回调和平台输入输出相关副作用，需保持公开契约稳定。
class QQWebSocketClient:
    """QQ WebSocket client — handles handshake, messaging, and keepalive."""

    # LLM: __init__ 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
    def __init__(self, url: str, access_token: str, intents: int = 1 << 30) -> None:
        self.url = url
        self.access_token = access_token
        self.intents = intents
        self._sock: _socket | None = None
        self._connected = False
        self._closed = False
        self._last_seq = 0

    # LLM: is_connected 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 判断connected条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
    @property
    def is_connected(self) -> bool:
        return self._connected

    # LLM: connect 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理connect相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
    def connect(self) -> None:
        """Establish TCP connection and complete WebSocket handshake."""
        parsed = _parse_ws_url(self.url)
        sock = _connect_socket(parsed)
        key = _send_handshake(sock, parsed, self.access_token)
        _verify_handshake(sock, key)
        self._sock = sock
        self._connected = True

    # LLM: send_text 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 发送文本请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def send_text(self, payload: str | bytes) -> None:
        """Send a text frame."""
        if not self._connected or self._sock is None:
            raise ConnectionError("Not connected")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        frame = WebSocketFrame.build_text_frame(payload, masked=True)
        self._sock.sendall(frame)

    # LLM: send_json 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 发送JSON请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def send_json(self, data: dict[str, Any]) -> None:
        """Send a JSON text frame."""
        self.send_text(json.dumps(data, ensure_ascii=False))

    # LLM: recv_text 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理recv文本相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
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

    # LLM: _handle_received_frame 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进receivedframe的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
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
            return payload.decode("utf-8")
        if opcode == WebSocketFrame.OPCODE_PING:
            self._send_pong()
        return None

    # LLM: _send_pong 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 发送pong请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def _send_pong(self) -> None:
        if self._sock:
            self._sock.sendall(bytes([0x8A, 0x00]))

    # LLM: close 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理close相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
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
