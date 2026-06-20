

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

_FEIHSU_API_BASE = "https://open.feishu.cn/open-apis"


class FeishuAdapter(BaseChannelAdapter):

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

        self.app_id = config.get("feishu_app_id", "")
        self.app_secret = config.get("feishu_app_secret", "")
        self.verification_token = config.get("feishu_verification_token", "")
        self.encrypt_key = config.get("feishu_encrypt_key", "")

        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._tenant_access_token: str | None = None
        self._token_expires_at: float = 0


    def start(self) -> None:
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
        if self._server is None:
            return
        try:
            self._server.serve_forever()
        except Exception as exc:
            # 原 except:pass 把异常静默吞掉 + _running 仍 True → 通道无声死亡而健康探测/supervisor 误判在线
            # (审计 #15)。记录异常并置 _running=False,让 running/状态检查感知它已死、可触发重启。
            logger.error(f"飞书回调服务器异常退出(通道已死,需重启): {type(exc).__name__}: {exc}")
            self._running = False

    def stop(self) -> None:
        if not self._running:
            return
        with self._lock:
            self._running = False
            self._stop_event.set()
            self._shutdown_server_async()
            self._join_server_thread()
            logger.info("飞书适配器已停止")

    def _shutdown_server_async(self) -> None:
        if not self._server:
            return
        try:
            t = threading.Thread(target=self._do_shutdown, daemon=True)
            t.start()
        except Exception as exc:
            logger.warning(f"关闭飞书 HTTP 服务异常: {exc}")
        self._server = None

    def _join_server_thread(self) -> None:
        if not self._server_thread:
            return
        self._server_thread.join(timeout=5)
        self._server_thread = None

    def _do_shutdown(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as exc:
                logger.warning(f"飞书 HTTP shutdown 异常: {exc}")


    def _handle_feishu_event(self, payload: dict[str, Any]) -> None:
        msg = feishu_to_incoming(payload)
        if msg is None:
            return
        self._dispatch(msg)


    def send_message(self, user_id: str, message: OutgoingMessage) -> bool:
        try:
            token = self._get_tenant_access_token()
            if not token:
                logger.error("飞书: 无法获取 tenant_access_token")
                return False

            result = self._post_feishu_message(user_id, message, token)
            if result.get("code") == 0:
                return True
            logger.error(f"飞书发送消息失败: {result}")
            return False

        except Exception as exc:
            logger.error(f"飞书 send_message 异常: {exc}")
            return False

    def _post_feishu_message(
        self,
        user_id: str,
        message: OutgoingMessage,
        token: str,
    ) -> dict[str, Any]:
        feishu_payload = outgoing_to_feishu(message)
        payload = {
            "receive_id": user_id,
            "msg_type": feishu_payload["msg_type"],
            "content": json.dumps(feishu_payload["content"], ensure_ascii=False),
        }
        req = urllib.request.Request(
            f"{_FEIHSU_API_BASE}/im/v1/messages?receive_id_type=open_id",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _get_tenant_access_token(self) -> str | None:
        now = time.time()
        if self._tenant_access_token and now < self._token_expires_at - 60:
            return self._tenant_access_token

        try:
            result = self._request_tenant_access_token()
            if result.get("code") == 0:
                self._tenant_access_token = result.get("tenant_access_token", "")
                self._token_expires_at = now + result.get("expire", 7200)
                return self._tenant_access_token
        except Exception as exc:
            logger.error(f"获取飞书 token 失败: {exc}")
        return None

    def _request_tenant_access_token(self) -> dict[str, Any]:
        url = f"{_FEIHSU_API_BASE}/auth/v3/tenant_access_token/internal"
        payload = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))


    def verify_feishu_signature(self, token: str, timestamp: str, signature: str) -> bool:
        import secrets

        # fail-closed:未配置任何验证手段时拒绝(不处理无验证事件,防伪造 webhook 驱动 agent)
        if not self.encrypt_key and not self.verification_token:
            return False
        if not self.encrypt_key:
            return secrets.compare_digest(token, self.verification_token)  # 常数时间比,防时序侧信道
        source = f"{self.encrypt_key}{timestamp}{token}"
        expected = hmac.new(source.encode(), b"", hashlib.sha256).hexdigest()
        return secrets.compare_digest(signature, expected)


class _FeishuCallbackHandler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass  # 静默日志

    def do_POST(self) -> None:
        if self.path != "/feishu/callback":
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8", "replace")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "invalid json"}')
            return

        if payload.get("type") == "url_verification":
            challenge = payload.get("challenge", "")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"challenge": challenge}).encode("utf-8"))
            return

        adapter: FeishuAdapter | None = getattr(self.server, "adapter", None)
        if adapter is None:
            self.send_response(500)
            self.end_headers()
            return

        token = self.headers.get("X-Lark-Verification-Token", "")
        timestamp = self.headers.get("X-Lark-Request-Timestamp", "")
        signature = self.headers.get("X-Lark-Signature", "")
        # 始终验签(原 `if token and` 在缺 token 头时会跳过验证 → 伪造事件可未认证驱动 agent)
        if not adapter.verify_feishu_signature(token, timestamp, signature):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error": "signature mismatch"}')
            return

        adapter._handle_feishu_event(payload)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"feishu adapter is running")


import urllib.request
