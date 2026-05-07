# LLM: External adapter module; keep platform payload and runtime boundary contracts stable.
# 模块用途: 对接 QQ、飞书等外部渠道，把平台事件转换成内部请求。


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


# LLM: FeishuAdapter 属于外部通道适配的类边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 类用途: 适配飞书adapter协议，把平台消息转换为内部统一消息契约；关键副作用: 方法可能触发通道配置、消息回调和平台输入输出相关副作用，需保持公开契约稳定。
class FeishuAdapter(BaseChannelAdapter):

    adapter_name = "feishu"

    # LLM: __init__ 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
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


    # LLM: start 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进start的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
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

    # LLM: _serve 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进serve的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    def _serve(self) -> None:
        if self._server is None:
            return
        try:
            self._server.serve_forever()
        except Exception:
            pass

    # LLM: stop 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进stop的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    def stop(self) -> None:
        if not self._running:
            return
        with self._lock:
            self._running = False
            self._stop_event.set()
            self._shutdown_server_async()
            self._join_server_thread()
            logger.info("飞书适配器已停止")

    # LLM: _shutdown_server_async 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理shutdownserverasync相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    def _shutdown_server_async(self) -> None:
        if not self._server:
            return
        try:
            t = threading.Thread(target=self._do_shutdown, daemon=True)
            t.start()
        except Exception as exc:
            logger.warning(f"关闭飞书 HTTP 服务异常: {exc}")
        self._server = None

    # LLM: _join_server_thread 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理joinserverthread相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    def _join_server_thread(self) -> None:
        if not self._server_thread:
            return
        self._server_thread.join(timeout=5)
        self._server_thread = None

    # LLM: _do_shutdown 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理doshutdown相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
    def _do_shutdown(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as exc:
                logger.warning(f"飞书 HTTP shutdown 异常: {exc}")


    # LLM: _handle_feishu_event 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 推进飞书event的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响通道配置、消息回调和平台输入输出，需保持重试、超时和状态迁移语义。
    def _handle_feishu_event(self, payload: dict[str, Any]) -> None:
        msg = feishu_to_incoming(payload)
        if msg is None:
            return
        self._dispatch(msg)


    # LLM: send_message 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 发送消息请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
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

    # LLM: _post_feishu_message 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 发送飞书消息请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
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
            return json.loads(resp.read().decode("utf-8"))

    # LLM: _get_tenant_access_token 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 读取或查询tenantaccess令牌需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
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

    # LLM: _request_tenant_access_token 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 发送tenantaccess令牌请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def _request_tenant_access_token(self) -> dict[str, Any]:
        url = f"{_FEIHSU_API_BASE}/auth/v3/tenant_access_token/internal"
        payload = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))


    # LLM: verify_feishu_signature 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理verify飞书signature相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
    def verify_feishu_signature(self, token: str, timestamp: str, signature: str) -> bool:
        if not self.encrypt_key:
            return token == self.verification_token
        import secrets

        source = f"{self.encrypt_key}{timestamp}{token}"
        expected = hmac.new(source.encode(), b"", hashlib.sha256).hexdigest()
        return secrets.compare_digest(signature, expected)


# LLM: _FeishuCallbackHandler 属于外部通道适配的类边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 类用途: 封装飞书callbackhandler相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发通道配置、消息回调和平台输入输出相关副作用，需保持公开契约稳定。
class _FeishuCallbackHandler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    # LLM: log_message 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 写入消息的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动通道配置、消息回调和平台输入输出，调用方依赖写入顺序和文件格式。
    def log_message(self, format: str, *args: Any) -> None:
        pass  # 静默日志

    # LLM: do_POST 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理dopost相关的数据流，连接当前职责的前后步骤；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def do_POST(self) -> None:
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
        if token and not adapter.verify_feishu_signature(token, timestamp, signature):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error": "signature mismatch"}')
            return

        adapter._handle_feishu_event(payload)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    # LLM: do_GET 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
    # 函数用途: 处理doget相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"feishu adapter is running")


import urllib.request
