"""LLM: QQ 通道适配器 — 通过 QQ 机器人开放平台接收和发送消息。

给人看的解释：
QQ 适配器使用轮询方式：
1. 定期调用 QQ Open API 获取频道最新消息
2. 提取文本内容后提交给 gateway 处理
3. gateway 结果通过 QQ API 发送回频道
使用线程实现后台轮询。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .base import BaseChannelAdapter
from .protocol import IncomingMessage, OutgoingMessage, qq_to_incoming, outgoing_to_qq

logger = logging.getLogger(__name__)

# QQ Open Platform API
_QQ_API_BASE = "https://api.sgroup.qq.com"


class QQAdapter(BaseChannelAdapter):
    """QQ 通道适配器。"""

    adapter_name = "qq"

    def __init__(
        self,
        config: dict[str, Any],
        workspace_root: Path | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        super().__init__(config)
        self.workspace_root = workspace_root or Path.cwd()

        # 配置
        self.app_id = config.get("qq_app_id", "")
        self.app_secret = config.get("qq_app_secret", "")
        self.token = config.get("qq_token", "")
        self.guild_id = config.get("qq_guild_id", "")
        self.channel_id = config.get("qq_channel_id", "")

        self.poll_interval = poll_interval

        # 轮询线程
        self._poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # 消息去重：记录最近处理过的 message_id
        self._seen_ids: set[str] = set()
        self._seen_lock = threading.Lock()

        # API token（懒获取）
        self._access_token: str | None = None
        self._token_expires_at: float = 0

    # -------------------------------------------------------------------------
    # 启停
    # -------------------------------------------------------------------------

    def start(self) -> None:
        """启动 QQ 轮询线程。"""
        if self._running:
            return
        with self._lock:
            if self._running:
                return
            self._stop_event.clear()
            self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._poll_thread.start()
            self._running = True
            logger.info(f"QQ 适配器已启动，轮询间隔={self.poll_interval}s")

    def stop(self) -> None:
        """停止 QQ 轮询线程。"""
        if not self._running:
            return
        with self._lock:
            self._running = False
            self._stop_event.set()
            if self._poll_thread:
                self._poll_thread.join(timeout=5)
                self._poll_thread = None
            logger.info("QQ 适配器已停止")

    # -------------------------------------------------------------------------
    # 轮询逻辑
    # -------------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """后台轮询 QQ 频道消息。"""
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                logger.warning(f"QQ 轮询异常: {exc}")
            self._stop_event.wait(self.poll_interval)

    def _poll_once(self) -> None:
        """单次轮询：拉取最新消息并处理。"""
        token = self._get_access_token()
        if not token:
            return

        if not self.channel_id:
            logger.warning("QQ 适配器未配置 channel_id，跳过轮询")
            return

        try:
            url = f"{_QQ_API_BASE}/channels/{self.channel_id}/messages"
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                messages = result.get("data", []) if isinstance(result, dict) else []
                for msg_data in messages:
                    self._process_qq_message({"d": msg_data})
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self._access_token = None  # token 过期，强制刷新
        except Exception as exc:
            logger.warning(f"QQ 拉取消息失败: {exc}")

    def _process_qq_message(self, payload: dict[str, Any]) -> None:
        """处理单条 QQ 消息，去重后分发给回调。"""
        msg = qq_to_incoming(payload)
        if msg is None:
            return
        with self._seen_lock:
            if msg.message_id in self._seen_ids:
                return
            self._seen_ids.add(msg.message_id)
            # 保留最近 1000 条 id 防止内存膨胀
            if len(self._seen_ids) > 1000:
                self._seen_ids = set(sorted(self._seen_ids)[-500:])
        self._dispatch(msg)

    # -------------------------------------------------------------------------
    # 消息发送
    # -------------------------------------------------------------------------

    def send_message(self, user_id: str, message: OutgoingMessage) -> bool:
        """通过 QQ 机器人向用户/频道发送消息。"""
        try:
            token = self._get_access_token()
            if not token:
                logger.error("QQ: 无法获取 access_token")
                return False

            channel_id = message.metadata.get("qq_channel_id", self.channel_id)
            if not channel_id:
                logger.error("QQ: 缺少 channel_id")
                return False

            qq_payload = outgoing_to_qq(message)
            url = f"{_QQ_API_BASE}/channels/{channel_id}/messages"
            payload = {
                "content": qq_payload["content"],
                "msg_type": 0,  # 0 = 文本消息
            }

            data = json.dumps(payload).encode("utf-8")
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

    def _get_access_token(self) -> str | None:
        """获取 QQ access_token，带缓存。"""
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token

        try:
            # QQ 使用 client_credentials 模式
            url = f"{_QQ_API_BASE}/oauth2/access_token"
            payload = json.dumps({
                "grant_type": "client_credentials",
                "client_id": self.app_id,
                "client_secret": self.app_secret,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if "access_token" in result:
                    self._access_token = result.get("access_token", "")
                    self._token_expires_at = now + result.get("expires_in", 7200)
                    return self._access_token
        except Exception as exc:
            logger.error(f"获取 QQ access_token 失败: {exc}")
        return None


import urllib.error
import urllib.request
