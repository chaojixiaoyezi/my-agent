

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..delivery import ChannelAdapterRegistry, DeliveryContext, DeliveryService, ReplyEnvelope
from .base import BaseChannelAdapter
from .delivery import GatewayReplyDeliveryStore, GatewayReplyDeliveryWorker, PendingGatewayReply
from .protocol import IncomingMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewayAskSubmission:
    """Gateway ingress result for one channel message."""

    request_id: str
    status: str = "queued"
    kind: str = ""
    ok: bool = True
    message: str = ""


# LLM: adapter registry 的健康时间统一使用带时区 UTC ISO，便于跨进程状态投影比较。
# 函数用途: 返回当前 UTC 时间文本。
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _render_gateway_progress(event: dict[str, object]) -> str:
    if str(event.get("kind") or "") == "assistant_commentary":
        return str(event.get("text") or "").strip()
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


# LLM: Provider idempotency is scoped to one logical channel message, not the whole Gateway request.
# 函数用途: 为同一入站消息的各进度批次和最终回复生成稳定且互不冲突的投递键。
def _gateway_delivery_key(
    *,
    message_id: str,
    request_id: str,
    phase: str,
    progress_cursor: int = 0,
) -> str:
    anchor = str(message_id or request_id or "").strip()
    request = str(request_id or "").strip()
    if not anchor:
        return ""
    if phase == "progress":
        return f"gateway-reply:{anchor}:{request}:progress:{max(0, int(progress_cursor))}"
    return f"gateway-reply:{anchor}:{request}:final"


# LLM: Gateway 回复仍经过唯一 DeliveryService；本 helper 只组装可信入站路由和逻辑消息身份。
# 函数用途: 在不扩张 ChannelManager 职责的前提下投递一条 Gateway 进度或最终回复。
def _deliver_gateway_message(
    delivery_service: DeliveryService,
    *,
    adapter_available: bool,
    msg: IncomingMessage,
    request_id: str,
    response_text: str,
    handle: str = "",
    idempotency_key: str = "",
) -> bool:
    if not adapter_available:
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
        idempotency_key=(
            str(idempotency_key or "").strip()
            or _gateway_delivery_key(
                message_id=msg.message_id,
                request_id=request_id,
                phase="final",
            )
        ),
    )
    receipt = delivery_service.deliver(context, ReplyEnvelope(content=response_text, format="text"))
    return receipt.delivery_status == "sent"


# LLM: durable pending 记录是后台回复路由的唯一事实，不从响应正文或当前会话状态重建身份。
# 函数用途: 把待回送记录还原为统一的可信入站路由对象。
def _pending_gateway_message(pending: PendingGatewayReply) -> IncomingMessage:
    return IncomingMessage(
        channel=pending.channel,
        user_id=pending.user_id,
        content="",
        message_id=pending.message_id,
        conversation_id=pending.conversation_id,
    )


def _gateway_ask_payload(msg: IncomingMessage) -> dict[str, object]:
    # 真实入站消息始终有 conversation_id；getattr 兼容旧的嵌入调用和轻量测试替身。
    conversation_id = str(getattr(msg, "conversation_id", "") or "").strip()
    incoming_metadata = getattr(msg, "metadata", {}) or {}
    chat_type = str(incoming_metadata.get("feishu_chat_type") or incoming_metadata.get("chat_type") or "").strip()
    chat_id = str(incoming_metadata.get("feishu_chat_id") or incoming_metadata.get("chat_id") or "").strip()
    metadata: dict[str, object] = {
        "channel": msg.channel,
        "user_id": msg.user_id,
        "message_id": msg.message_id,
        "adapter": msg.channel,
        "channel_conversation_id": conversation_id,
    }
    if chat_type:
        metadata["channel_chat_type"] = chat_type
    if chat_id:
        metadata["channel_chat_id"] = chat_id
    payload: dict[str, object] = {
        "kind": "ask",
        "prompt": msg.content,
        "metadata": metadata,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
        payload["channel_conversation_id"] = conversation_id
    return payload


# LLM: Adapter identity headers are sourced only from the trusted incoming message structure.
# 函数用途：为普通请求和控制请求生成同一组 Gateway 身份头。
def _gateway_identity_headers(msg: IncomingMessage) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if msg.user_id:
        headers["X-User-Id"] = str(msg.user_id)
    if msg.channel:
        headers["X-Channel"] = str(msg.channel)
    return headers


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
            poll_response=lambda pending: self._poll_gateway_once(pending, interval=0.0),
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
        self._delivery_registry.register_adapter(name, adapter, configured=True)
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
                self._delivery_registry.mark_health(
                    adapter.adapter_name,
                    "healthy",
                    checked_at=_utc_now_iso(),
                )
                continue
            self._delivery_registry.mark_health(
                adapter.adapter_name,
                "starting",
                checked_at=_utc_now_iso(),
            )
            try:
                adapter.start()
            except Exception as exc:
                self._delivery_registry.mark_health(
                    adapter.adapter_name,
                    "unhealthy",
                    checked_at=_utc_now_iso(),
                    error_code="CHANNEL_ADAPTER_START_FAILED",
                )
                logger.error(f"启动适配器 {adapter.adapter_name} 失败: {exc}")
                continue
            self._delivery_registry.mark_health(
                adapter.adapter_name,
                "healthy" if adapter.running else "unhealthy",
                checked_at=_utc_now_iso(),
                error_code="" if adapter.running else "CHANNEL_ADAPTER_NOT_RUNNING",
            )

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
                self._delivery_registry.mark_health(
                    adapter.adapter_name,
                    "unhealthy",
                    checked_at=_utc_now_iso(),
                    error_code="CHANNEL_ADAPTER_STOP_FAILED",
                )
                logger.error(f"停止适配器 {adapter.adapter_name} 失败: {exc}")
                continue
            self._delivery_registry.mark_health(
                adapter.adapter_name,
                "stopped",
                checked_at=_utc_now_iso(),
            )

    # LLM: adapter 进程状态文件只能序列化 registry 的脱敏快照，不复制另一套状态判断。
    # 函数用途: 返回所有已注册通道的生命周期和能力状态，供跨进程能力诊断读取。
    def runtime_channel_statuses(self) -> list[dict[str, object]]:
        return [item.to_dict() for item in self._delivery_registry.runtime_snapshot()]

    # -------------------------------------------------------------------------
    # 消息路由
    # -------------------------------------------------------------------------

    def route_message(self, msg: IncomingMessage) -> bool:
        """提交外部消息并登记后台回送；不在飞书/WS 回调线程等待模型结果。"""
        import urllib.error

        try:
            self._maybe_download_media(msg)  # 入站图片/文件下载到工作区,content 注入路径(供 agent 看图/读文件)
            submission = self._submit_gateway_ask(msg)
            request_id = submission.request_id
            if submission.status == "control":
                request_id = request_id or f"control-{msg.message_id}"
                if submission.kind == "stop":
                    if submission.ok:
                        self._discard_interrupted_reply(msg, submission.request_id)
                    # `/stop` is the IM equivalent of pressing a stop button:
                    # the command itself never creates another chat message.
                    # Invalid syntax is rejected before this branch and may
                    # still return usage help.
                    return True
                return self._send_gateway_reply(
                    msg,
                    request_id,
                    submission.message or "系统命令没有返回结果。",
                )
            if not request_id:
                return False
            # 会话运行时 active-turn steer is decided by the Gateway, not by
            # this IM adapter.  The ordinary message was durably attached to
            # the already-running turn, whose existing
            # delivery record owns subsequent model commentary/final output;
            # adding another record for the same request would overwrite the
            # original reply envelope and strand its progress indicator.
            if submission.status == "steered":
                return True
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

    # LLM: Match both request and trusted channel scope before suppressing a late reply.
    # 函数用途: `/stop` 成功后丢弃当前会话该请求的旧回复并撤掉占位提示。
    def _discard_interrupted_reply(self, msg: IncomingMessage, request_id: str) -> None:
        records = self._reply_delivery.discard_where(
            lambda record: (
                record.request_id == request_id
                and record.channel == msg.channel
                and record.user_id == msg.user_id
                and record.conversation_id == msg.conversation_id
            ),
            reason="conversation_user_stop",
        )
        adapter = self._adapters.get(msg.channel)
        if adapter is None:
            return
        for record in records:
            adapter.clear_progress_placeholder(record.user_id, record.progress_handle)

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

    def _submit_gateway_ask(self, msg: IncomingMessage) -> GatewayAskSubmission:
        import urllib.request

        payload = _gateway_ask_payload(msg)
        # 转发真实渠道身份(走 127.0.0.1 回环=网关可信来源):渠道用户拿到自己的身份/USER 角色,
        # 不再因"缺头"被当本机终端 admin(审计 #2:渠道用户全跑成 admin)。
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.gateway_port}/ask",
            data=json.dumps(payload).encode("utf-8"),
            headers=_gateway_identity_headers(msg),
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8", "replace"))
        request_id = str(result.get("request_id") or "").strip()
        status = str(result.get("status") or "queued").strip().lower()
        if not request_id and status != "control":
            logger.error(f"gateway /ask 未返回 request_id: {result}")
        return GatewayAskSubmission(
            request_id=request_id,
            status=status,
            kind=str(result.get("kind") or "").strip().lower(),
            ok=bool(result.get("ok", True)),
            message=str(result.get("message") or ""),
        )

    # LLM: 最终回复与显式消息共用 DeliveryService；当前 channel/user/reply_to 只能取入站可信结构。
    # 函数用途: 把 Gateway 最终正文装入无收件人的 ReplyEnvelope，并回复原用户。
    def _send_gateway_reply(
        self,
        msg: IncomingMessage,
        request_id: str,
        response_text: str,
        handle: str = "",
        *,
        idempotency_key: str = "",
    ) -> bool:
        sent = _deliver_gateway_message(
            self._delivery_service,
            adapter_available=self._adapters.get(msg.channel) is not None,
            msg=msg,
            request_id=request_id,
            response_text=response_text,
            handle=handle,
            idempotency_key=idempotency_key,
        )
        if sent:
            self._update_active_channel(msg.user_id, msg.channel)
        return sent

    def _deliver_gateway_reply(self, pending: PendingGatewayReply, response_text: str) -> bool:
        return self._send_gateway_reply(
            _pending_gateway_message(pending),
            pending.request_id,
            response_text,
            pending.progress_handle,
        )

    def _deliver_gateway_progress(self, pending: PendingGatewayReply, response_text: str) -> bool:
        return self._send_gateway_reply(
            _pending_gateway_message(pending),
            pending.request_id,
            response_text,
            idempotency_key=_gateway_delivery_key(
                message_id=pending.message_id,
                request_id=pending.request_id,
                phase="progress",
                progress_cursor=pending.progress_cursor,
            ),
        )

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

    def _poll_gateway_once(self, pending: PendingGatewayReply, interval: float) -> str | None:
        """Poll one reply through the same trusted owner identity as its inbound message."""
        import urllib.error
        import urllib.request

        try:
            url = f"http://127.0.0.1:{self.gateway_port}/result/{pending.request_id}"
            req = urllib.request.Request(
                url,
                headers={"X-User-Id": pending.user_id, "X-Channel": pending.channel},
            )
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
