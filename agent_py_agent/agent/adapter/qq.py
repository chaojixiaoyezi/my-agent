

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
from .protocol import IncomingMessage, OutgoingMessage, qq_to_incoming
from .qq_runtime import (
    close_ws,
    handle_ready_event,
    join_ws_threads,
    request_reconnect,
    send_heartbeat_once,
)
from .qq_ws import QQWebSocketClient

logger = logging.getLogger(__name__)

_QQ_API_BASE = "https://api.sgroup.qq.com"
_QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

def _qq_send_message(self, user_id: str, message: OutgoingMessage) -> bool:
    try:
        token = self._get_access_token()
        if not token:
            logger.error("QQ: 无法获取 access_token")
            return False
        url, http_payload = _qq_send_target(user_id, message)
        if not url:
            logger.error("QQ: 缺少 channel_id（需从事件 metadata 中获取）")
            return False
        req = urllib.request.Request(
            url,
            data=json.dumps(http_payload).encode("utf-8"),
            headers={"Authorization": f"QQBot {token}", "Content-Type": "application/json"},
        )
        ok, result = _qq_send_request(req)
        if ok:
            return True
        logger.error(f"QQ 发送消息失败: {result}")
        return False
    except Exception as exc:
        logger.error(f"QQ send_message 异常: {exc}")
        return False

def _qq_send_request(req: urllib.request.Request) -> tuple[bool, dict[str, Any]]:
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        return result.get("code") == 0 or resp.status == 200, result

def _parse_ws_payload(raw: str) -> dict[str, Any] | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None

def _qq_send_target(user_id: str, message: OutgoingMessage) -> tuple[str, dict[str, object]]:
    user_openid = message.metadata.get("qq_user_openid", user_id)
    payload = {"content": message.content[:4000], "msg_type": 0}
    if user_openid:
        return f"{_QQ_API_BASE}/v2/users/{user_openid}/messages", payload
    channel_id = message.metadata.get("qq_channel_id", "")
    if not channel_id:
        return "", payload
    return f"{_QQ_API_BASE}/channels/{channel_id}/messages", payload

def _qq_get_access_token(self) -> str | None:
    now = time.time()
    if self._access_token and now < self._token_expires_at - 60:
        return self._access_token
    try:
        result = _qq_request_token(self.app_id, self.app_secret)
        logger.debug(f"QQ token response: {result}")
        if "access_token" not in result:
            logger.error(f"QQ token 响应缺少 access_token: {result}")
            return None
        self._access_token = result["access_token"]
        self._token_expires_at = now + float(result.get("expires_in", 7200))
        return self._access_token
    except Exception as exc:
        logger.error(f"获取 QQ access_token 失败: {exc}")
        return None

def _qq_request_token(app_id: str, app_secret: str) -> dict[str, Any]:
    http_payload = json.dumps({
        "appId": str(app_id),
        "clientSecret": str(app_secret),
    }).encode("utf-8")
    req = urllib.request.Request(
        _QQ_TOKEN_URL,
        data=http_payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _qq_fetch_gateway_url(self, token: str) -> str | None:
    try:
        req = urllib.request.Request(
            f"{_QQ_API_BASE}/gateway",
            headers={"Authorization": f"QQBot {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("url")
    except Exception as exc:
        logger.error(f"获取 QQ gateway URL 失败: {exc}")
        return None

class QQAdapter(BaseChannelAdapter):

    adapter_name = "qq"

    def __init__(
        self,
        config: dict[str, Any],
        workspace_root: Path | None = None,
    ) -> None:
        super().__init__(config)
        self.workspace_root = workspace_root or Path.cwd()

        self.app_id = config.get("qq_app_id", "")
        self.app_secret = config.get("qq_app_secret", "")

        self._ws: QQWebSocketClient | None = None
        self._ws_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._seen_ids: set[str] = set()
        self._seen_lock = threading.Lock()

        self._access_token: str | None = None
        self._token_expires_at: float = 0
        self._gateway_url: str | None = None
        self._heartbeat_interval: float = 0  # 毫秒

        self._session_id: str | None = None
        self._resume_url: str | None = None
        self._reconnect_count = 0
        self._max_reconnect = 5

    def start(self) -> None:
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
        if not self._running:
            return
        with self._lock:
            self._running = False
            self._stop_event.set()
            self._close_ws()
            self._join_ws_threads()
            logger.info("QQ 适配器已停止")

    def _run_ws_loop(self) -> None:
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
        token = self._get_access_token()
        if not token:
            raise ConnectionError("无法获取 QQ access_token")
        self._access_token = token

        gateway_url = self._fetch_gateway_url(token)
        if not gateway_url:
            raise ConnectionError("无法获取 QQ WebSocket gateway URL")

        ws = QQWebSocketClient(gateway_url, token)
        ws.connect()
        self._ws = ws

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

        self._reconnect_count = 0  # 连接成功，重置计数
        while not self._stop_event.is_set():
            msg = ws.recv_text(timeout=5.0)
            if msg is None:
                continue
            self._handle_ws_message(msg)

    def _intents_for_qq(self) -> int:
        return (1 << 25) | (1 << 30) | (1 << 12)

    def _handle_ws_message(self, raw: str) -> None:
        payload = _parse_ws_payload(raw)
        if payload is None:
            return

        op = payload.get("op", -1)
        d = payload.get("d", {})
        if op == 0:
            self._handle_dispatch_event(payload, d)
            return
        if op == 1:
            self._maybe_start_heartbeat(d)
            return
        if op == 7:
            logger.warning("QQ WebSocket 要求重连")
            self._session_id = None
            self._stop_event.set()

    def _handle_dispatch_event(self, payload: dict[str, Any], d: dict[str, Any]) -> None:
        seq = payload.get("s", 0)
        if seq:
            self._last_seq = seq
        t = payload.get("t", "")
        if t in {"MESSAGE_CREATE", "C2C_MESSAGE_CREATE"}:
            self._process_qq_message(d)
            return
        if t == "READY":
            self._handle_ready_event(d)
            return
        if t == "INVALID_SESSION":
            self._request_reconnect("QQ WebSocket INVALID_SESSION，重新连接")

    def _maybe_start_heartbeat(self, d: dict[str, Any]) -> None:
        raw_hb = d.get("heartbeat_interval", 0)
        if raw_hb and raw_hb > 0:
            self._heartbeat_interval = float(raw_hb)
            self._start_heartbeat()

    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        interval_sec = self._heartbeat_interval / 1000.0
        if interval_sec <= 0:
            return

        def heartbeat_loop() -> None:
            while not self._stop_event.is_set():
                self._send_heartbeat_once()
                self._stop_event.wait(interval_sec)

        self._heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _process_qq_message(self, d: dict[str, Any]) -> None:
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

    send_message = _qq_send_message
    _get_access_token = _qq_get_access_token
    _fetch_gateway_url = _qq_fetch_gateway_url
    _close_ws = close_ws
    _join_ws_threads = join_ws_threads
    _handle_ready_event = handle_ready_event
    _request_reconnect = request_reconnect
    _send_heartbeat_once = send_heartbeat_once
