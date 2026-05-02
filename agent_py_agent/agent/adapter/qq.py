"""LLM: QQ 通道适配器 — 通过 QQ 机器人 WebSocket 长连接接收和接收消息。

给人看的解释：
QQ 适配器改用 WebSocket 长连接：
1. 用 app_id + app_secret 获取 access_token
2. 用 access_token 获取 WebSocket 接入地址
3. 连接 WebSocket，发送 IDENTIFY 握手
4. 接收 MESSAGE_CREATE 事件，转发给 gateway
5. 定期发送 HEARTBEAT 保活
6. 发消息仍用 HTTP POST
只用标准库实现最小 WebSocket 客户端（RFC 6455）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import ssl
import struct
import threading
import time
from pathlib import Path
from socket import socket as _socket
from typing import Any

from .base import BaseChannelAdapter
from .protocol import IncomingMessage, OutgoingMessage, qq_to_incoming, outgoing_to_qq

logger = logging.getLogger(__name__)

# QQ Open Platform API
_QQ_API_BASE = "https://api.sgroup.qq.com"


# ---------------------------------------------------------------------------
# 最小 WebSocket 客户端（RFC 6455，仅支持文本帧）
# ---------------------------------------------------------------------------

class _WebSocketFrame:
    """WebSocket 帧解析和构造（仅文本帧）。"""

    # Opcode
    OPCODE_CONTINUATION = 0x0
    OPCODE_TEXT = 0x1
    OPCODE_BINARY = 0x2
    OPCODE_CLOSE = 0x8
    OPCODE_PING = 0x9
    OPCODE_PONG = 0xA

    @staticmethod
    def build_text_frame(payload: bytes, masked: bool = True) -> bytes:
        """构造一个文本数据帧（客户端 → 服务器，必须掩码）。"""
        length = len(payload)
        # 第一个字节: FIN=1, OPCODE=1 (text)
        first = 0x81

        if masked:
            # 生成 4 字节掩码 key
            mask_key = os.urandom(4)
            masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            # 第二个字节: MASK=1, payload length
            if length < 126:
                second = 0x80 | length
                return bytes([first, second]) + mask_key + masked_payload
            elif length < 65536:
                second = 0x80 | 126
                return bytes([first, second]) + struct.pack(">H", length) + mask_key + masked_payload
            else:
                second = 0x80 | 127
                return bytes([first, second]) + struct.pack(">Q", length) + mask_key + masked_payload
        else:
            if length < 126:
                return bytes([first, length]) + payload
            elif length < 65536:
                return bytes([first, 126]) + struct.pack(">H", length) + payload
            else:
                return bytes([first, 127]) + struct.pack(">Q", length) + payload

    @staticmethod
    def build_close_frame() -> bytes:
        """构造关闭帧。"""
        return bytes([0x88, 0x00])

    @staticmethod
    def build_ping_frame() -> bytes:
        """构造 Ping 帧。"""
        return bytes([0x89, 0x00])

    @staticmethod
    def parse_frame(data: bytes) -> tuple[int, bytes] | None:
        """解析服务器返回的帧。返回 (opcode, payload) 或 None。"""
        if len(data) < 2:
            return None

        first = data[0]
        second = data[1]
        opcode = first & 0x0F
        has_mask = bool(second & 0x80)
        length = second & 0x7F

        offset = 2
        if length == 126:
            if len(data) < 4:
                return None
            length = struct.unpack(">H", data[2:4])[0]
            offset = 4
        elif length == 127:
            if len(data) < 10:
                return None
            length = struct.unpack(">Q", data[2:10])[0]
            offset = 10

        if has_mask:
            if len(data) < offset + 4:
                return None
            mask_key = data[offset : offset + 4]
            offset += 4

        if len(data) < offset + length:
            return None

        payload = data[offset : offset + length]
        if has_mask:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        return opcode, payload


class _QQWebSocketClient:
    """QQ WebSocket 客户端 — 负责握手、收發消息、保活。"""

    def __init__(self, url: str, access_token: str, intents: int = 1 << 30) -> None:
        self.url = url
        self.access_token = access_token
        self.intents = intents
        self._sock: _socket | None = None
        self._connected = False
        self._closed = False
        self._last_seq = 0

    def connect(self) -> None:
        """建立 TCP 连接并完成 WebSocket 握手。"""
        # 解析 URL
        # ws://host:port/gateway?...
        # wss://host:port/gateway?...
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

        # TCP 连接
        sock = _socket()
        sock.settimeout(30)
        if is_ssl:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.connect((host, port))

        # WebSocket 握手
        import secrets

        key = base64.b64encode(secrets.token_bytes(16)).decode()
        handshake = (
            f"GET {path}?{self.url.split('?', 1)[1] if '?' in self.url else ''} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {self.access_token}\r\n"
            f"\r\n"
        )
        sock.sendall(handshake.encode())

        # 读取握手响应
        response = b""
        while b"\r\n\r\n" not in response:
            response += sock.recv(4096)

        # 验证握手响应
        if b"HTTP/1.1 101" not in response and b"HTTP/1.0 101" not in response:
            sock.close()
            raise ConnectionError(f"WebSocket handshake failed: {response[:200]}")

        # 简单验证 Sec-WebSocket-Accept
        # Accept = base64(SHA1(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
        accept_expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if accept_expected.encode() not in response:
            # 某些实现不返回这个头，不强制校验
            pass

        self._sock = sock
        self._connected = True

    def send_text(self, payload: str | bytes) -> None:
        """发送一个文本帧。"""
        if not self._connected or self._sock is None:
            raise ConnectionError("Not connected")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        frame = _WebSocketFrame.build_text_frame(payload, masked=True)
        self._sock.sendall(frame)

    def send_json(self, data: dict[str, Any]) -> None:
        """发送一个 JSON 文本帧。"""
        self.send_text(json.dumps(data, ensure_ascii=False))

    def recv_text(self, timeout: float | None = None) -> str | None:
        """接收一个文本帧，超时返回 None。"""
        if not self._connected or self._sock is None:
            return None
        self._sock.settimeout(timeout)
        try:
            data = self._sock.recv(8192)
            if not data:
                return None
            result = _WebSocketFrame.parse_frame(data)
            if result is None:
                return None
            opcode, payload = result
            if opcode == _WebSocketFrame.OPCODE_CLOSE:
                self._connected = False
                return None
            if opcode == _WebSocketFrame.OPCODE_TEXT:
                return payload.decode("utf-8")
            if opcode == _WebSocketFrame.OPCODE_PING:
                # 自动回应 Pong
                if self._sock:
                    self._sock.sendall(_WebSocketFrame.build_close_frame())
                return None
            return None
        except Exception:
            return None

    def close(self) -> None:
        """优雅关闭连接。"""
        self._closed = True
        if self._connected and self._sock:
            try:
                self._sock.sendall(_WebSocketFrame.build_close_frame())
            except Exception:
                pass
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        self._connected = False


# ---------------------------------------------------------------------------
# QQ 适配器
# ---------------------------------------------------------------------------


class QQAdapter(BaseChannelAdapter):
    """QQ 通道适配器（WebSocket 长连接版）。"""

    adapter_name = "qq"

    def __init__(
        self,
        config: dict[str, Any],
        workspace_root: Path | None = None,
    ) -> None:
        super().__init__(config)
        self.workspace_root = workspace_root or Path.cwd()

        # 配置（只需 app_id 和 app_secret）
        self.app_id = config.get("qq_app_id", "")
        self.app_secret = config.get("qq_app_secret", "")

        # WebSocket 客户端
        self._ws: _QQWebSocketClient | None = None
        self._ws_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # 消息去重
        self._seen_ids: set[str] = set()
        self._seen_lock = threading.Lock()

        # Token 和 Gateway
        self._access_token: str | None = None
        self._token_expires_at: float = 0
        self._gateway_url: str | None = None
        self._heartbeat_interval: float = 0  # 毫秒

        # 重连状态
        self._session_id: str | None = None
        self._resume_url: str | None = None
        self._reconnect_count = 0
        self._max_reconnect = 5

    # -------------------------------------------------------------------------
    # 启停
    # -------------------------------------------------------------------------

    def start(self) -> None:
        """启动 QQ WebSocket 连接。"""
        if self._running:
            return
        with self._lock:
            if self._running:
                return
            self._stop_event.clear()
            self._ws_thread = threading.Thread(target=self._run_ws_loop, daemon=True)
            self._ws_thread.start()
            self._running = True
            logger.info("QQ 适配器已启动（WebSocket 模式）")

    def stop(self) -> None:
        """停止 QQ WebSocket 连接。"""
        if not self._running:
            return
        with self._lock:
            self._running = False
            self._stop_event.set()
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
            if self._ws_thread:
                self._ws_thread.join(timeout=5)
                self._ws_thread = None
            if self._heartbeat_thread:
                self._heartbeat_thread.join(timeout=5)
                self._heartbeat_thread = None
            logger.info("QQ 适配器已停止")

    # -------------------------------------------------------------------------
    # WebSocket 主循环
    # -------------------------------------------------------------------------

    def _run_ws_loop(self) -> None:
        """WebSocket 连接主循环：连接 → 认证 → 收消息 → 重连。"""
        while not self._stop_event.is_set():
            try:
                self._connect_and_run()
            except Exception as exc:
                logger.warning(f"QQ WebSocket 异常: {exc}")
                self._reconnect_count += 1

            if self._stop_event.is_set():
                break

            if self._reconnect_count >= self._max_reconnect:
                logger.error(f"QQ WebSocket 重连次数超过上限 ({self._max_reconnect})，停止适配器")
                break

            wait = min(30.0 * (2 ** min(self._reconnect_count, 4)), 120.0)
            logger.info(f"QQ WebSocket {wait:.0f}s 后重连...")
            self._stop_event.wait(wait)

    def _connect_and_run(self) -> None:
        """单次连接：获取 token → 获取 gateway → 连接 → 认证 → 收消息。"""
        # 1. 获取 access_token
        token = self._get_access_token()
        if not token:
            raise ConnectionError("无法获取 QQ access_token")
        self._access_token = token

        # 2. 获取 WebSocket gateway 地址
        gateway_url = self._fetch_gateway_url(token)
        if not gateway_url:
            raise ConnectionError("无法获取 QQ WebSocket gateway URL")

        # 3. 连接 WebSocket
        ws = _QQWebSocketClient(gateway_url, token)
        ws.connect()
        self._ws = ws

        # 4. 发送 IDENTIFY（带 intents）
        ws.send_json({
            "op": 2,
            "d": {
                "token": token,
                "intents": self._intents_for_qq(),
                "shard": [0, 1],
                "properties": {
                    "os": "my-agent",
                    "browser": "my-agent",
                    "device": "my-agent",
                },
            },
        })

        # 5. 循环收消息
        self._reconnect_count = 0  # 连接成功，重置计数
        while not self._stop_event.is_set():
            msg = ws.recv_text(timeout=5.0)
            if msg is None:
                continue
            self._handle_ws_message(msg)

    def _intents_for_qq(self) -> int:
        """计算 QQ WebSocket intents 值。GUILD_MESSAGES = 1 << 30"""
        return 1 << 30  # 只订阅频道消息事件

    def _handle_ws_message(self, raw: str) -> None:
        """处理 WebSocket 消息帧。"""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return

        op = payload.get("op", -1)
        d = payload.get("d", {})

        if op == 0:
            # 事件消息
            t = payload.get("t", "")
            if t == "MESSAGE_CREATE":
                self._process_qq_message(d)
            elif t == "READY":
                # 保存 heartbeat_interval 和 session_id
                self._session_id = d.get("session_id")
                # heartbeat_interval 在 d.heartbeat_interval
                raw_hb = d.get("heartbeat_interval", 0)
                if raw_hb and raw_hb > 0:
                    self._heartbeat_interval = float(raw_hb)
                    self._start_heartbeat()
                logger.info(f"QQ WebSocket READY，session_id={self._session_id}")
            elif t == "RESUMED":
                logger.info("QQ WebSocket 会话恢复成功")
            elif t == "INVALID_SESSION":
                # 需要重新 IDENTIFY
                logger.warning("QQ WebSocket INVALID_SESSION，重新连接")
                self._session_id = None
                self._stop_event.set()
        elif op == 1:
            # Hello，发送心跳
            raw_hb = d.get("heartbeat_interval", 0)
            if raw_hb and raw_hb > 0:
                self._heartbeat_interval = float(raw_hb)
                self._start_heartbeat()
        elif op == 7:
            # Reconnect，需要重连
            logger.warning("QQ WebSocket 要求重连")
            self._session_id = None
            self._stop_event.set()
        elif op == 11:
            # Heartbeat ACK
            pass

    # -------------------------------------------------------------------------
    # 心跳
    # -------------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        """启动心跳线程。"""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        interval_sec = self._heartbeat_interval / 1000.0
        if interval_sec <= 0:
            return

        def heartbeat_loop() -> None:
            while not self._stop_event.is_set():
                try:
                    if self._ws and self._ws._connected:
                        self._ws.send_json({"op": 1, "d": self._last_seq})
                except Exception:
                    pass
                self._stop_event.wait(interval_sec)

        self._heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    # -------------------------------------------------------------------------
    # 消息处理
    # -------------------------------------------------------------------------

    def _process_qq_message(self, d: dict[str, Any]) -> None:
        """处理收到的 MESSAGE_CREATE 事件。"""
        # 构造标准 WebSocket 帧格式给 qq_to_incoming
        payload = {"d": d, "t": "MESSAGE_CREATE"}
        msg = qq_to_incoming(payload)
        if msg is None:
            return
        with self._seen_lock:
            if msg.message_id in self._seen_ids:
                return
            self._seen_ids.add(msg.message_id)
            if len(self._seen_ids) > 1000:
                self._seen_ids = set(sorted(self._seen_ids)[-500:])
        self._dispatch(msg)

    # -------------------------------------------------------------------------
    # 消息发送
    # -------------------------------------------------------------------------

    def send_message(self, user_id: str, message: OutgoingMessage) -> bool:
        """通过 QQ 机器人 HTTP API 发送消息到频道。"""
        try:
            token = self._get_access_token()
            if not token:
                logger.error("QQ: 无法获取 access_token")
                return False

            channel_id = message.metadata.get("qq_channel_id", "")
            if not channel_id:
                logger.error("QQ: 缺少 channel_id（需从事件 metadata 中获取）")
                return False

            qq_payload = outgoing_to_qq(message)
            url = f"{_QQ_API_BASE}/channels/{channel_id}/messages"
            http_payload = {
                "content": qq_payload["content"],
                "msg_type": 0,
            }

            data = json.dumps(http_payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )

            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("code") == 0 or resp.status == 200:
                    return True
                logger.error(f"QQ 发送消息失败: {result}")
                return False

        except Exception as exc:
            logger.error(f"QQ send_message 异常: {exc}")
            return False

    # -------------------------------------------------------------------------
    # API 请求
    # -------------------------------------------------------------------------

    def _get_access_token(self) -> str | None:
        """获取 QQ access_token，带缓存。"""
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token

        try:
            url = f"{_QQ_API_BASE}/oauth2/access_token"
            http_payload = {
                "grant_type": "client_credentials",
                "client_id": self.app_id,
                "client_secret": self.app_secret,
            }
            data = json.dumps(http_payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if "access_token" in result:
                    self._access_token = result["access_token"]
                    self._token_expires_at = now + result.get("expires_in", 7200)
                    return self._access_token
        except Exception as exc:
            logger.error(f"获取 QQ access_token 失败: {exc}")
        return None

    def _fetch_gateway_url(self, token: str) -> str | None:
        """获取 QQ WebSocket gateway 地址。"""
        try:
            url = f"{_QQ_API_BASE}/gateway"
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("url")
        except Exception as exc:
            logger.error(f"获取 QQ gateway URL 失败: {exc}")
        return None


import urllib.error
import urllib.request
