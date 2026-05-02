"""LLM: 飞书（Lark）通道适配器 — 通过飞书机器人接收和发送消息。

给人看的解释：
飞书适配器同时做两件事：
1. 启动一个本地 HTTP 回调服务（接收飞书的事件推送）
2. 通过飞书 webhook API 发送消息
使用线程实现回调服务 + 轮询的并发。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from .base import BaseChannelAdapter
from .protocol import IncomingMessage, OutgoingMessage, feishu_to_incoming, outgoing_to_feishu

logger = logging.getLogger(__name__)

# 飞书 API 端点
_FEIHSU_API_BASE = "https://open.feishu.cn/open-apis"


class FeishuAdapter(BaseChannelAdapter):
    """飞书通道适配器。"""

    adapter_name = "feishu"

    def __init__(
        self,
        config: dict[str, Any],
        callback_port: int = 8421,
        workspace_root: Path | None = None,
    ) -> None:
        super().__init__(config)
        self.callback_port = callback_port
        self.workspace_root = workspace_root or Path.cwd()

        # 配置
        self.app_id = config.get("feishu_app_id", "")
        self.app_secret = config.get("feishu_app_secret", "")
        self.verification_token = config.get("feishu_verification_token", "")
        self.encrypt_key = config.get("feishu_encrypt_key", "")

        # HTTP 服务
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # 飞书 API token（懒获取）
        self._tenant_access_token: str | None = None
        self._token_expires_at: float = 0

    # -------------------------------------------------------------------------
    # 启停
    # -------------------------------------------------------------------------

    def start(self) -> None:
        """启动飞书 HTTP 回调服务。"""
        if self._running:
            return
        with self._lock:
            if self._running:
                return
            self._stop_event.clear()
            self._server = ThreadingHTTPServer(
                ("0.0.0.0", self.callback_port),
                _FeishuCallbackHandler,
            )
            self._server.adapter = self  # type: ignore[attr-defined]
            self._server.daemon_threads = True
            self._server_thread = threading.Thread(target=self._serve, daemon=True)
            self._server_thread.start()
            self._running = True
            logger.info(f"飞书适配器已启动，回调端口={self.callback_port}")

    def _serve(self) -> None:
        """HTTP 服务主循环。"""
        if self._server is None:
            return
        try:
            self._server.serve_forever()
        except Exception:
            pass

    def stop(self) -> None:
        """停止飞书回调服务。"""
        if not self._running:
            return
        with self._lock:
            self._running = False
            self._stop_event.set()
            if self._server:
                try:
                    # shutdown() 会让 serve_forever() 退出，在新线程里调用避免卡住主线程
                    t = threading.Thread(target=self._do_shutdown, daemon=True)
                    t.start()
                except Exception as exc:
                    logger.warning(f"关闭飞书 HTTP 服务异常: {exc}")
                self._server = None
            if self._server_thread:
                self._server_thread.join(timeout=5)
                self._server_thread = None
            logger.info("飞书适配器已停止")

    def _do_shutdown(self) -> None:
        """在线程里执行 server shutdown 和 close。"""
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as exc:
                logger.warning(f"飞书 HTTP shutdown 异常: {exc}")

    # -------------------------------------------------------------------------
    # 消息接收（飞书回调）
    # -------------------------------------------------------------------------

    def _handle_feishu_event(self, payload: dict[str, Any]) -> None:
        """处理飞书回调事件，转换为 IncomingMessage 并分发给回调。"""
        msg = feishu_to_incoming(payload)
        if msg is None:
            return
        self._dispatch(msg)

    # -------------------------------------------------------------------------
    # 消息发送
    # -------------------------------------------------------------------------

    def send_message(self, user_id: str, message: OutgoingMessage) -> bool:
        """通过飞书机器人向用户发送消息。"""
        try:
            # 获取 tenant_access_token
            token = self._get_tenant_access_token()
            if not token:
                logger.error("飞书: 无法获取 tenant_access_token")
                return False

            # 构造发送请求 — 发给用户（open_id）
            # 飞书机器人发消息需要先获取用户的 open_id，或通过会话 ID
            feishu_payload = outgoing_to_feishu(message)
            url = f"{_FEIHSU_API_BASE}/im/v1/messages?receive_id_type=open_id"
            payload = {
                "receive_id": user_id,
                "msg_type": feishu_payload["msg_type"],
                "content": json.dumps(feishu_payload["content"], ensure_ascii=False),
            }

            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {token}",
                },
            )

            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("code") == 0:
                    return True
                logger.error(f"飞书发送消息失败: {result}")
                return False

        except Exception as exc:
            logger.error(f"飞书 send_message 异常: {exc}")
            return False

    def _get_tenant_access_token(self) -> str | None:
        """获取飞书 tenant_access_token，带缓存。"""
        now = time.time()
        if self._tenant_access_token and now < self._token_expires_at - 60:
            return self._tenant_access_token

        try:
            url = f"{_FEIHSU_API_BASE}/auth/v3/tenant_access_token/internal"
            payload = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("code") == 0:
                    self._tenant_access_token = result.get("tenant_access_token", "")
                    self._token_expires_at = now + result.get("expire", 7200)
                    return self._tenant_access_token
        except Exception as exc:
            logger.error(f"获取飞书 token 失败: {exc}")
        return None

    # -------------------------------------------------------------------------
    # 安全验证
    # -------------------------------------------------------------------------

    def verify_feishu_signature(self, token: str, timestamp: str, signature: str) -> bool:
        """校验飞书事件签名的有效性。"""
        if not self.encrypt_key:
            # 没配置加密 key 时只校验 verification_token
            return token == self.verification_token
        # 飞书 sign 计算：encrypt_key + timestamp + token 拼成字符串后 SHA256
        import secrets

        source = f"{self.encrypt_key}{timestamp}{token}"
        expected = hmac.new(source.encode(), b"", hashlib.sha256).hexdigest()
        return secrets.compare_digest(signature, expected)


class _FeishuCallbackHandler(BaseHTTPRequestHandler):
    """飞书 HTTP 回调处理器。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass  # 静默日志

    def do_POST(self) -> None:
        """接收飞书事件回调。"""
        if self.path != "/feishu/callback":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "invalid json"}')
            return

        # 验证 URL 挑战（飞书配置回调地址时的握手）
        if payload.get("type") == "url_verification":
            challenge = payload.get("challenge", "")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"challenge": challenge}).encode("utf-8"))
            return

        # 消息事件
        adapter: FeishuAdapter | None = getattr(self.server, "adapter", None)
        if adapter is None:
            self.send_response(500)
            self.end_headers()
            return

        # 安全验证（可选）
        token = self.headers.get("X-Lark-Verification-Token", "")
        timestamp = self.headers.get("X-Lark-Request-Timestamp", "")
        signature = self.headers.get("X-Lark-Signature", "")
        if token and not adapter.verify_feishu_signature(token, timestamp, signature):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error": "signature mismatch"}')
            return

        # 处理事件
        adapter._handle_feishu_event(payload)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def do_GET(self) -> None:
        """飞书配置回调地址时发送 GET 请求进行握手验证。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"feishu adapter is running")


import urllib.request
