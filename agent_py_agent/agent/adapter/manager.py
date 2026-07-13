

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ..delivery import ChannelAdapterRegistry, DeliveryContext, DeliveryService, ReplyEnvelope
from .base import BaseChannelAdapter
from .delivery import GatewayReplyDeliveryStore, GatewayReplyDeliveryWorker, PendingGatewayReply
from .protocol import IncomingMessage

logger = logging.getLogger(__name__)


def _render_gateway_progress(event: dict[str, object]) -> str:
    tool = str(event.get("tool") or "工具")
    status = str(event.get("status") or "").strip()
    elapsed = event.get("elapsed_seconds")
    elapsed_text = f"（{float(elapsed):.2f} 秒）" if isinstance(elapsed, (int, float)) else ""
    if status == "开始":
        message = f"正在执行：{tool}"
    elif status.startswith("失败"):
        message = f"执行失败：{tool} {status}{elapsed_text}"
    elif status == "完成":
        message = f"执行完成：{tool}{elapsed_text}"
    else:
        message = f"执行进度：{tool} {status}{elapsed_text}".strip()
    detail = str(event.get("detail") or "").strip()
    if detail:
        message += f"\n{detail}"
    if str(event.get("level") or "") == "full":
        output = str(event.get("output") or "").strip()
        if output:
            message += f"\n结果：\n{output}"
    return message


def _gateway_ask_payload(msg: IncomingMessage) -> dict[str, object]:
    # 真实入站消息始终有 conversation_id；getattr 兼容旧的嵌入调用和轻量测试替身。
    conversation_id = str(getattr(msg, "conversation_id", "") or "").strip()
    payload: dict[str, object] = {
        "kind": "ask",
        "prompt": msg.content,
        "metadata": {
            "channel": msg.channel,
            "user_id": msg.user_id,
            "message_id": msg.message_id,
            "adapter": msg.channel,
            "channel_conversation_id": conversation_id,
        },
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
        payload["channel_conversation_id"] = conversation_id
    return payload


class ChannelManager:
    """管理所有已注册的通道适配器，提供统一的启停和消息路由接口。"""

    def __init__(
        self,
        gateway_port: int = 8420,
        *,
        delivery_state_dir: Path | None = None,
        delivery_poll_interval: float = 1.0,
    ) -> None:
        self._adapters: dict[str, BaseChannelAdapter] = {}
        self._delivery_registry = ChannelAdapterRegistry()
        self._delivery_service = DeliveryService(self._delivery_registry)
        self.gateway_port = gateway_port
        self._session_channel_file: Path | None = None  # 用于持久化活跃通道
        self._lifecycle_started = False
        self._reply_delivery = GatewayReplyDeliveryWorker(
            GatewayReplyDeliveryStore(delivery_state_dir),
            poll_response=lambda request_id: self._poll_gateway_once(request_id, interval=0.0),
            deliver_response=self._deliver_gateway_reply,
            poll_progress=self._poll_gateway_progress,
            deliver_progress=self._deliver_gateway_progress,
            poll_interval=delivery_poll_interval,
        )

    @property
    def session_channel_file(self) -> Path | None:
        """返回活跃通道存储文件路径。"""
        return self._session_channel_file

    @session_channel_file.setter
    def session_channel_file(self, path: Path | None) -> None:
        """设置活跃通道存储文件路径（供测试和外部注入）。"""
        self._session_channel_file = path

    # -------------------------------------------------------------------------
    # 适配器注册
    # -------------------------------------------------------------------------

    def register_adapter(self, adapter: BaseChannelAdapter) -> None:
        """注册一个通道适配器。"""
        name = adapter.adapter_name
        if name in self._adapters:
            logger.warning(f"适配器 {name} 已注册，将被替换")
        self._adapters[name] = adapter
        self._delivery_registry.register_adapter(name, adapter)
        logger.info(f"已注册通道适配器: {name}")

    def get_adapter(self, name: str) -> BaseChannelAdapter | None:
        """获取指定名称的适配器。"""
        return self._adapters.get(name)

    def list_adapters(self) -> list[str]:
        """列出所有已注册的适配器名称。"""
        return list(self._adapters.keys())

    # -------------------------------------------------------------------------
    # 启停
    # -------------------------------------------------------------------------

    def start_all(self) -> None:
        """启动所有已注册的适配器。"""
        self._lifecycle_started = True
        # 持久化队列必须在新入站消息到来前恢复；无状态的测试/嵌入调用没有待投递时不白起线程。
        if self._reply_delivery.store.durable or self._reply_delivery.store.pending():
            self._reply_delivery.start()
        for adapter in self._adapters.values():
            if adapter.running:
                continue
            try:
                adapter.start()
            except Exception as exc:
                logger.error(f"启动适配器 {adapter.adapter_name} 失败: {exc}")

    def stop_all(self) -> None:
        """停止所有已注册的适配器。"""
        self._lifecycle_started = False
        # 先停回送线程，再断开通道；未完成记录仍在磁盘，下次 start_all 会继续。
        self._reply_delivery.stop()
        for adapter in self._adapters.values():
            if not adapter.running:
                continue
            try:
                adapter.stop()
            except Exception as exc:
                logger.error(f"停止适配器 {adapter.adapter_name} 失败: {exc}")

    # -------------------------------------------------------------------------
    # 消息路由
    # -------------------------------------------------------------------------

    def route_message(self, msg: IncomingMessage) -> bool:
        """提交外部消息并登记后台回送；不在飞书/WS 回调线程等待模型结果。"""
        import urllib.error

        try:
            self._maybe_download_media(msg)  # 入站图片/文件下载到工作区,content 注入路径(供 agent 看图/读文件)
            request_id = self._submit_gateway_ask(msg)
            if not request_id:
                return False
            # 提交后立即给"处理中"反馈(飞书=给消息贴 reaction;其他通道默认空=跳过),让用户秒见反馈;
            # 拿到可撤销句柄,完成后撤掉反馈再发结果。
            adapter = self._adapters.get(msg.channel)
            if adapter is None:
                logger.error(f"找不到 channel={msg.channel} 的适配器")
                return False
            handle = adapter.send_progress_placeholder(msg.user_id, msg.message_id)
            self._reply_delivery.enqueue(
                PendingGatewayReply(
                    request_id=request_id,
                    channel=msg.channel,
                    user_id=msg.user_id,
                    message_id=msg.message_id,
                    conversation_id=str(getattr(msg, "conversation_id", "") or "").strip(),
                    progress_handle=handle,
                    created_at=time.time(),
                )
            )
            if self._lifecycle_started:
                self._reply_delivery.start()
            return True
        except urllib.error.URLError as exc:
            logger.error(f"gateway 请求失败: {exc}")
            return False
        except Exception as exc:
            logger.error(f"route_message 异常: {exc}")
            return False

    def _maybe_download_media(self, msg: IncomingMessage) -> None:
        """入站图片/文件下载到 adapter 工作区,content 注入绝对路径(agent 可据此 analyze_image/读文件)。
        失败静默(不影响消息处理);无 media / 通道不支持媒体直接跳过。"""
        media = (msg.metadata or {}).get("media")
        adapter = self._adapters.get(msg.channel)
        root = getattr(adapter, "workspace_root", None)
        if not media or root is None or not hasattr(adapter, "fetch_media_to"):
            return
        try:
            dest = Path(root) / "inbound_media"
            name = adapter.fetch_media_to(msg.message_id, media, dest)
            if name:
                msg.content = f"{msg.content}\n[已下载到: {dest / name}]"
        except Exception as exc:
            logger.warning(f"入站媒体下载失败(不影响处理): {exc}")

    def _submit_gateway_ask(self, msg: IncomingMessage) -> str:
        import urllib.request

        payload = _gateway_ask_payload(msg)
        # 转发真实渠道身份(走 127.0.0.1 回环=网关可信来源):渠道用户拿到自己的身份/USER 角色,
        # 不再因"缺头"被当本机终端 admin(审计 #2:渠道用户全跑成 admin)。
        headers = {"Content-Type": "application/json"}
        if msg.user_id:
            headers["X-User-Id"] = str(msg.user_id)
        if msg.channel:
            headers["X-Channel"] = str(msg.channel)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.gateway_port}/ask",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8", "replace"))
        request_id = result.get("request_id", "")
        if not request_id:
            logger.error(f"gateway /ask 未返回 request_id: {result}")
        return request_id

    # LLM: 最终回复与显式消息共用 DeliveryService；当前 channel/user/reply_to 只能取入站可信结构。
    # 函数用途: 把 Gateway 最终正文装入无收件人的 ReplyEnvelope，并回复原用户。
    def _send_gateway_reply(self, msg: IncomingMessage, request_id: str, response_text: str, handle: str = "") -> bool:
        if self._adapters.get(msg.channel) is None:
            logger.error(f"找不到 channel={msg.channel} 的适配器")
            return False
        context = DeliveryContext(
            channel=msg.channel,
            target=msg.user_id,
            mode="reply",
            conversation_id=msg.conversation_id,
            reply_to=msg.message_id,
            progress_handle=handle,
            request_id=request_id,
        )
        receipt = self._delivery_service.deliver(context, ReplyEnvelope(content=response_text, format="text"))
        if receipt.delivery_status == "sent":
            self._update_active_channel(msg.user_id, msg.channel)
            return True
        return False

    def _deliver_gateway_reply(self, pending: PendingGatewayReply, response_text: str) -> bool:
        msg = IncomingMessage(
            channel=pending.channel,
            user_id=pending.user_id,
            content="",
            message_id=pending.message_id,
            conversation_id=pending.conversation_id,
        )
        return self._send_gateway_reply(
            msg,
            pending.request_id,
            response_text,
            pending.progress_handle,
        )

    def _deliver_gateway_progress(self, pending: PendingGatewayReply, response_text: str) -> bool:
        msg = IncomingMessage(
            channel=pending.channel,
            user_id=pending.user_id,
            content="",
            message_id=pending.message_id,
            conversation_id=pending.conversation_id,
        )
        return self._send_gateway_reply(msg, pending.request_id, response_text)

    def _poll_gateway_progress(self, pending: PendingGatewayReply) -> tuple[list[str], int]:
        import urllib.request

        url = (
            f"http://127.0.0.1:{self.gateway_port}/progress/{pending.request_id}"
            f"?since={pending.progress_cursor}"
        )
        headers = {"X-User-Id": pending.user_id, "X-Channel": pending.channel}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        events = body.get("events") if isinstance(body, dict) else []
        messages = [
            rendered
            for event in (events if isinstance(events, list) else [])
            if isinstance(event, dict) and (rendered := _render_gateway_progress(event))
        ]
        return messages, max(pending.progress_cursor, int(body.get("next") or 0))

    def _poll_gateway_once(self, request_id: str, interval: float) -> str | None:
        """Poll gateway once; return response string, error string, or None to retry."""
        import urllib.error
        import urllib.request

        try:
            url = f"http://127.0.0.1:{self.gateway_port}/result/{request_id}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            if resp.status == 200 and body.get("ok"):
                return body.get("response", "")
            if resp.status == 200 and "error" in body:
                return f"错误: {body.get('error', 'unknown')}"
            time.sleep(interval)
            return None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                time.sleep(interval)
                return None
            return f"HTTP 错误: {exc.code}"
        except Exception:
            time.sleep(interval)
            return None

    # -------------------------------------------------------------------------
    # 活跃通道查询
    # -------------------------------------------------------------------------

    def get_active_channel(self, user_id: str) -> str | None:
        """查询用户当前活跃的通道。"""
        if self._session_channel_file is None:
            return None
        try:
            if not self._session_channel_file.exists():
                return None
            data = json.loads(self._session_channel_file.read_text(encoding="utf-8"))
            return data.get(user_id)
        except (json.JSONDecodeError, OSError):
            return None

    def _update_active_channel(self, user_id: str, channel: str) -> None:
        """更新用户当前活跃通道到本地文件。"""

        if self._session_channel_file is None:
            return
        try:
            data = {}
            if self._session_channel_file.exists():
                data = json.loads(self._session_channel_file.read_text(encoding="utf-8"))
            data[user_id] = channel
            self._session_channel_file.parent.mkdir(parents=True, exist_ok=True)
            self._session_channel_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"更新活跃通道失败: {exc}")

    def update_active_channel(self, user_id: str, channel: str) -> None:
        """公开的更新活跃通道方法。"""
        self._update_active_channel(user_id, channel)
