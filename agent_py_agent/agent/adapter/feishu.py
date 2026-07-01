

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlopen

from .base import BaseChannelAdapter
from .feishu_media import (
    MAX_FILE_BYTES,
    MAX_IMAGE_BYTES,
    build_multipart,
    file_message_type,
    file_upload_type,
    safe_media_filename,
)
from .feishu_render import build_outbound_payload, split_message, strip_markdown_to_plain_text
from .feishu_typing import FeishuTypingMixin
from .protocol import IncomingMessage, OutgoingMessage, feishu_to_incoming

logger = logging.getLogger(__name__)

_FEIHSU_API_BASE = "https://open.feishu.cn/open-apis"


def _feishu_msg_api(method: str, path: str, body: dict[str, Any], token: str) -> dict[str, Any]:
    """统一飞书消息 API 调用(urllib);method=POST/PUT,body→JSON。返回响应 dict。"""
    req = urllib.request.Request(
        f"{_FEIHSU_API_BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _send_with_fallback(text: str, send: Callable[[str, str], dict[str, Any]]) -> bool:
    """渲染 + 发(send(msg_type, content_json)→响应);post 富文本被判格式错则剥 markdown 回落 text 重发。"""
    msg_type, content_json = build_outbound_payload(text)
    result = send(msg_type, content_json)
    if result.get("code") == 0:
        return True
    if msg_type == "post":
        fallback = json.dumps({"text": strip_markdown_to_plain_text(text)}, ensure_ascii=False)
        result = send("text", fallback)
        if result.get("code") == 0:
            return True
    logger.error(f"飞书发送失败: {result}")
    return False


def _send_feishu_rendered(user_id: str, text: str, token: str) -> bool:
    """普通发送一条(markdown→post/否则 text;post 格式错回落 text)。"""
    return _send_with_fallback(text, lambda mt, cj: _feishu_msg_api(
        "POST", "/im/v1/messages?receive_id_type=open_id",
        {"receive_id": user_id, "msg_type": mt, "content": cj}, token))


def _reply_feishu_rendered(reply_to: str, text: str, token: str) -> bool:
    """引用回复某条消息(渲染 + /reply 端点;post 格式错回落)。"""
    return _send_with_fallback(text, lambda mt, cj: _feishu_msg_api(
        "POST", f"/im/v1/messages/{reply_to}/reply", {"msg_type": mt, "content": cj}, token))


def _edit_feishu_rendered(message_id: str, text: str, token: str) -> bool:
    """编辑已发消息(PUT,流式落版/订正;post 格式错回落,编辑同一条)。"""
    return _send_with_fallback(text, lambda mt, cj: _feishu_msg_api(
        "PUT", f"/im/v1/messages/{message_id}", {"msg_type": mt, "content": cj}, token))


def _upload_feishu(path: Path, kind: str, fields: dict[str, str], token: str) -> str | None:
    """multipart 上传(kind=image/file),返回 image_key/file_key;超限/失败返回 None。"""
    max_bytes = MAX_IMAGE_BYTES if kind == "image" else MAX_FILE_BYTES
    if path.stat().st_size > max_bytes:
        logger.error(f"飞书上传超大小上限: {path.name}")
        return None
    body, content_type = build_multipart(fields, kind, path.name, path.read_bytes())
    req = urllib.request.Request(f"{_FEIHSU_API_BASE}/im/v1/{kind}s", data=body,
        headers={"Content-Type": content_type, "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8", "replace"))
    data = result.get("data") or {}
    key = str(data.get("image_key") or data.get("file_key") or "")
    if key:
        return key
    logger.error(f"飞书上传失败: {result.get('msg')}")
    return None


def _send_feishu_media(user_id: str, msg_type: str, content: dict[str, str], token: str) -> bool:
    """发图片/文件/音视频消息(content 含 image_key/file_key)。"""
    result = _feishu_msg_api("POST", "/im/v1/messages?receive_id_type=open_id",
        {"receive_id": user_id, "msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)}, token)
    if result.get("code") == 0:
        return True
    logger.error(f"飞书媒体消息发送失败: {result.get('msg')}")
    return False


def _send_feishu_image(user_id: str, path: Path, token: str) -> bool:
    key = _upload_feishu(path, "image", {"image_type": "message"}, token)
    return bool(key) and _send_feishu_media(user_id, "image", {"image_key": key}, token)


def _send_feishu_file(user_id: str, path: Path, token: str) -> bool:
    fields = {"file_type": file_upload_type(path), "file_name": path.name}
    key = _upload_feishu(path, "file", fields, token)
    return bool(key) and _send_feishu_media(user_id, file_message_type(path), {"file_key": key}, token)


def _fetch_feishu_resource(message_id: str, key: str, kind: str, token: str) -> bytes | None:
    """下载入站媒体 bytes;失败 None。"""
    req = urllib.request.Request(
        f"{_FEIHSU_API_BASE}/im/v1/messages/{message_id}/resources/{key}?type={kind}",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as exc:
        logger.warning(f"飞书媒体下载失败: {exc}")
        return None


def _fetch_media_to_dir(message_id: str, media: dict[str, str], dest_dir: Path, token: str) -> str | None:
    """下载入站媒体落盘 dest_dir,返回文件名;无媒体/失败 None。"""
    if media.get("image_key"):
        key, kind, suffix = str(media["image_key"]), "image", ".png"
    elif media.get("file_key"):
        key, kind, suffix = str(media["file_key"]), "file", ".bin"
    else:
        return None
    blob = _fetch_feishu_resource(message_id, key, kind, token)
    if not blob:
        return None
    name = safe_media_filename(message_id, media, key, suffix)
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / name).write_bytes(blob)
    return name


def _reply_after_card_action(adapter: Any, norm: dict[str, Any]) -> None:
    """飞书人设确认卡片回调:落写/取消(apply_card_action 核心逻辑),再回一条确认消息。回给待确认记录里的
    发起人(token 权威绑定发起人,群聊里别人点也只写发起人自己的文件);无记录(取消/失效)则回点击人。
    fail-open,绝不抛回长连。"""
    try:
        from .feishu_card import apply_card_action

        result = apply_card_action(norm.get("value") or {}, Path(adapter.my_agent_home))
        reply_to = str(result.get("owner_id") or norm.get("operator_open_id") or "")
        text = str(result.get("reply_text") or "")
        token = adapter._get_tenant_access_token() if (text and reply_to) else None
        if token:
            _send_feishu_rendered(reply_to, text, token)
    except Exception as exc:
        logger.error(f"飞书卡片回调处理异常(不影响长连): {type(exc).__name__}: {exc}")


class FeishuAdapter(FeishuTypingMixin, BaseChannelAdapter):

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
        # my_agent_home 根:卡片按钮回调据此读待确认记录、定位 owner 的 SOUL/AGENTS.md(与网关同一根)。
        self.my_agent_home = str(config.get("my_agent_home", "") or "")
        # 连接模式:webhook(默认,需公网回调地址)/ long_connection(长连接 WS,主动连飞书、免公网、内网可用)
        self.connection_mode = str(config.get("feishu_connection_mode", "webhook") or "webhook").strip().lower()
        self.ws_proxy = config.get("feishu_ws_proxy", "")

        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
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
            if self._is_long_connection():
                self._start_long_connection()
            else:
                self._start_webhook_server()
            self._running = True

    def _is_long_connection(self) -> bool:
        return self.connection_mode in ("long_connection", "longconn", "ws", "websocket")

    def _start_webhook_server(self) -> None:
        self._server = ThreadingHTTPServer(
            ("0.0.0.0", self.callback_port),
            _FeishuCallbackHandler,
        )
        self._server.adapter = self  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._server_thread = threading.Thread(target=self._serve, daemon=True)
        self._server_thread.start()
        logger.info(f"飞书适配器已启动(webhook 模式)，回调端口={self.callback_port}")

    def _start_long_connection(self) -> None:
        # 长连接 WS:主动连飞书网关(免公网/不绑端口),事件走与 webhook 同一条下游(_handle_feishu_event);
        # 异常退出回调置 _running=False,让健康探测/supervisor 感知通道死、触发重启。
        from .feishu_ws import FeishuWsClient, run_ws_client_thread

        self._ws_client = FeishuWsClient(
            app_id=self.app_id, app_secret=self.app_secret,
            on_payload=self._handle_feishu_event, ws_proxy=self.ws_proxy,
        )
        # 有 my_agent_home 才挂卡片回调(否则无处定位待确认记录/人格文件);无则不注册,行为不变。
        self._ws_client.on_card_action = self._handle_card_action if self.my_agent_home else None
        self._ws_thread = run_ws_client_thread(self._ws_client, lambda: setattr(self, "_running", False))
        logger.info("飞书适配器已启动(长连接 WS 模式,免公网/不绑端口)")

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
            self._shutdown_server_async()  # webhook 模式关 HTTP server(长连模式 _server=None,空操作)
            self._join_server_thread()
            self._stop_ws_client()  # 长连模式:尽力关闭 ws 客户端(lark 无干净 stop,daemon 线程随进程退出)
            logger.info("飞书适配器已停止")

    def _stop_ws_client(self) -> None:
        client, self._ws_client = self._ws_client, None
        if client is not None:
            client.close()  # 尽力关闭底层 ws(关不掉就靠 daemon 线程随进程退出)

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

    def _handle_card_action(self, norm: dict[str, Any]) -> None:
        """飞书人设确认卡片按钮回调(长连):落写/取消 + 回确认消息(逻辑在模块级 _reply_after_card_action)。"""
        _reply_after_card_action(self, norm)


    def send_message(self, user_id: str, message: OutgoingMessage) -> bool:
        try:
            token = self._get_tenant_access_token()
            if not token:
                logger.error("飞书: 无法获取 tenant_access_token")
                return False
            # markdown→post 渲染 + 长消息(>8000字符)分片;逐片全发(list 不短路),任一失败即整体失败。
            pieces = split_message(message.content)
            return all([_send_feishu_rendered(user_id, p, token) for p in pieces])
        except Exception as exc:
            logger.error(f"飞书 send_message 异常: {exc}")
            return False

    def reply_message(self, message_id: str, text: str) -> bool:
        """引用回复某条消息(markdown 渲染 + post 回落)。"""
        token = self._get_tenant_access_token()
        return bool(token) and _reply_feishu_rendered(message_id, text, token)

    def edit_message(self, message_id: str, text: str) -> bool:
        """编辑已发消息(流式落版/订正;markdown 渲染 + post 回落)。"""
        token = self._get_tenant_access_token()
        return bool(token) and _edit_feishu_rendered(message_id, text, token)

    def send_image(self, user_id: str, path: Path) -> bool:
        """上传并发送图片。"""
        token = self._get_tenant_access_token()
        return bool(token) and _send_feishu_image(user_id, path, token)

    def send_file(self, user_id: str, path: Path) -> bool:
        """上传并发送文件(按扩展名路由类型)。"""
        token = self._get_tenant_access_token()
        return bool(token) and _send_feishu_file(user_id, path, token)

    def fetch_media_to(self, message_id: str, media: dict[str, str], dest_dir: Path) -> str | None:
        """下载入站媒体到 dest_dir,返回文件名;无媒体/失败 None。"""
        token = self._get_tenant_access_token()
        return _fetch_media_to_dir(message_id, media, dest_dir, token) if token else None

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
