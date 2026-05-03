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

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .base import BaseChannelAdapter
from .protocol import IncomingMessage, OutgoingMessage, outgoing_to_qq, qq_to_incoming
from .qq_protocol import WebSocketFrame
from .qq_ws import QQWebSocketClient

logger = logging.getLogger(__name__)

# QQ Open Platform API
_QQ_API_BASE = "https://api.sgroup.qq.com"
_QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"


class QQAdapter(BaseChannelAdapter):
    """QQ 通道适配器（WebSocket 长连接版）— thin facade delegating to service classes."""

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
        self._ws: QQWebSocketClient | None = None
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
                logger.warning(f"QQ WebSocket 异常: {exc}", exc_info=True)
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
        ws = QQWebSocketClient(gateway_url, token)
        ws.connect()
        self._ws = ws

        # 4. 发送 IDENTIFY（带 intents）
        ws.send_json({
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
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
        """计算 QQ WebSocket intents 值。订阅私聊 + 频道@ + 公开频道。"""
        return (1 << 25) | (1 << 30) | (1 << 12)

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
            seq = payload.get("s", 0)
            if seq:
                self._last_seq = seq
            t = payload.get("t", "")
            if t == "MESSAGE_CREATE" or t == "C2C_MESSAGE_CREATE":
                self._process_qq_message(d)
            elif t == "READY":
                self._session_id = d.get("session_id")
                raw_hb = d.get("heartbeat_interval", 0)
                if raw_hb and raw_hb > 0:
                    self._heartbeat_interval = float(raw_hb)
                    self._start_heartbeat()
            elif t == "INVALID_SESSION":
                logger.warning("QQ WebSocket INVALID_SESSION，重新连接")
                self._session_id = None
                self._stop_event.set()
        elif op == 1:
            raw_hb = d.get("heartbeat_interval", 0)
            if raw_hb and raw_hb > 0:
                self._heartbeat_interval = float(raw_hb)
                self._start_heartbeat()
        elif op == 7:
            logger.warning("QQ WebSocket 要求重连")
            self._session_id = None
            self._stop_event.set()

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
                    if self._ws and self._ws.is_connected:
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
        payload = {"d": d, "t": "MESSAGE_CREATE"}
        msg = qq_to_incoming(payload)
        if msg is None:
            return
        user_openid = d.get("author", {}).get("user_openid", "")
        if user_openid:
            msg.metadata["qq_user_openid"] = user_openid
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
        """通过 QQ 机器人 HTTP API 发送消息到用户或频道。"""
        try:
            token = self._get_access_token()
            if not token:
                logger.error("QQ: 无法获取 access_token")
                return False

            # 私聊：用 user_openid 发到 /v2/users/{openid}/messages
            user_openid = message.metadata.get("qq_user_openid", user_id)
            if user_openid:
                url = f"{_QQ_API_BASE}/v2/users/{user_openid}/messages"
                http_payload = {
                    "content": message.content[:4000],
                    "msg_type": 0,
                }
            else:
                channel_id = message.metadata.get("qq_channel_id", "")
                if not channel_id:
                    logger.error("QQ: 缺少 channel_id（需从事件 metadata 中获取）")
                    return False
                url = f"{_QQ_API_BASE}/channels/{channel_id}/messages"
                http_payload = {
                    "content": message.content[:4000],
                    "msg_type": 0,
                }

            data = json.dumps(http_payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": f"QQBot {token}",
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
            http_payload = json.dumps({
                "appId": str(self.app_id),
                "clientSecret": str(self.app_secret),
            }).encode("utf-8")
            req = urllib.request.Request(
                _QQ_TOKEN_URL,
                data=http_payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                logger.debug(f"QQ token response: {result}")
                if "access_token" in result:
                    self._access_token = result["access_token"]
                    expires_in = result.get("expires_in", 7200)
                    self._token_expires_at = now + float(expires_in)
                    return self._access_token
                logger.error(f"QQ token 响应缺少 access_token: {result}")
        except Exception as exc:
            logger.error(f"获取 QQ access_token 失败: {exc}")
        return None

    def _fetch_gateway_url(self, token: str) -> str | None:
        """获取 QQ WebSocket gateway 地址。"""
        try:
            url = f"{_QQ_API_BASE}/gateway"
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"QQBot {token}"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("url")
        except Exception as exc:
            logger.error(f"获取 QQ gateway URL 失败: {exc}")
        return None