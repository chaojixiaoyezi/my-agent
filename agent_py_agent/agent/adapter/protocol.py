"""LLM: 通道适配器消息协议 — 统一外部消息格式与各平台转换函数。

给人看的解释：
飞书、QQ 等外部平台的消息格式各不相同，这里定义统一的内部格式
（IncomingMessage / OutgoingMessage），以及各平台到统一格式的转换函数。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IncomingMessage:
    """从外部通道收到的统一消息格式。"""

    channel: str
    user_id: str
    content: str
    message_id: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutgoingMessage:
    """发送给外部通道的统一消息格式。"""

    channel: str
    user_id: str
    content: str
    format: str = "text"  # "text" or "markdown"
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 飞书消息转换
# ---------------------------------------------------------------------------

FEISHU_MESSAGE_TYPES = {"text", "post", "image", "audio", "video", "file", "sticker", "share_chat", "system"}


def feishu_to_incoming(payload: dict[str, Any]) -> IncomingMessage | None:
    """把飞书回调 payload 转换成 IncomingMessage。

    飞书事件回调格式（简化版）：
    {
        "schema": "2.0",
        "header": {"event_id": "...", "event_type": "im.message.receive_v1", ...},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_xxx"}, "sender_type": "user"},
            "message": {
                "message_id": "om_xxx",
                "create_time": "1234567890",
                "chat_id": "oc_xxx",
                "content": "{\"text\":\"hello\"}",
            }
        }
    }
    """
    try:
        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {})
        open_id = sender_id.get("open_id", "")

        # content 是 JSON 字符串，需要解析
        raw_content = message.get("content", "{}")
        try:
            content_obj = json.loads(raw_content)
        except (json.JSONDecodeError, TypeError):
            content_obj = {"text": raw_content}

        # 提取纯文本
        text = content_obj.get("text", "").strip()

        # 只有文本消息才处理
        msg_type = message.get("msg_type", "text")
        if msg_type != "text" or not text:
            return None

        return IncomingMessage(
            channel="feishu",
            user_id=open_id,
            content=text,
            message_id=message.get("message_id", ""),
            timestamp=float(message.get("create_time", time.time())),
            metadata={
                "feishu_chat_id": message.get("chat_id", ""),
                "feishu_msg_type": msg_type,
                "feishu_raw": content_obj,
            },
        )
    except (ValueError, KeyError, TypeError):
        return None


def outgoing_to_feishu(msg: OutgoingMessage) -> dict[str, Any]:
    """把 OutgoingMessage 转换成飞书 webhook 发送格式。"""
    return {
        "msg_type": "text",
        "content": {"text": msg.content},
    }


# ---------------------------------------------------------------------------
# QQ 消息转换
# ---------------------------------------------------------------------------

def qq_to_incoming(payload: dict[str, Any]) -> IncomingMessage | None:
    """把 QQ 机器人回调 payload 转换成 IncomingMessage。

    支持两种格式：
    1. QQ 官方 WebSocket 事件（t= MESSAGE_CREATE）：
       {"t": "MESSAGE_CREATE", "d": {"id": "...", "channel_id": "...",
         "content": "...", "author": {"user_openid": "..."}, "timestamp": "..."}}
    2. 旧版简化格式（字段在 event 或 d 里）：
       {"event": {...}} 或 {"d": {"author": {"id": "..."}, ...}}
    """
    try:
        # event_type = payload.get("t", "")  # 新格式用 t 字段
        d = payload.get("d", {})
        if d:
            # 优先用 user_openid（QQ 官方 WebSocket 格式）
            author = d.get("author", {})
            user_id = str(
                author.get("user_openid", "")
                or author.get("id", "")
                or d.get("user_id", "")
            )
            content = d.get("content", "").strip()
            msg_id = d.get("id", d.get("msg_id", ""))
            raw_ts = d.get("timestamp", time.time())
            # timestamp 可以是 ISO 字符串或数字
            try:
                if isinstance(raw_ts, str):
                    ts = _parse_iso_timestamp(raw_ts)
                else:
                    ts = float(raw_ts)
            except Exception:
                ts = time.time()
            metadata = {
                "qq_guild_id": d.get("guild_id", ""),
                "qq_channel_id": d.get("channel_id", ""),
            }
        else:
            # 简化版格式
            event = payload.get("event", {})
            user_id = str(event.get("user_id", ""))
            content = event.get("content", "").strip()
            msg_id = event.get("msg_id", "")
            ts = float(event.get("timestamp", time.time()))
            metadata = {
                "qq_guild_id": event.get("guild_id", ""),
                "qq_channel_id": event.get("channel_id", ""),
            }

        if not content:
            return None

        return IncomingMessage(
            channel="qq",
            user_id=user_id,
            content=content,
            message_id=str(msg_id),
            timestamp=ts,
            metadata=metadata,
        )
    except (ValueError, KeyError, TypeError):
        return None


def _parse_iso_timestamp(ts_str: str) -> float:
    """解析 ISO 格式时间字符串为 Unix 时间戳。"""
    # "2024-01-01T00:00:00+08:00" → float
    ts_str = ts_str.strip()
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    # 移除时区，只保留 UTC 时间
    if "+" in ts_str:
        ts_str = ts_str.split("+")[0]
    elif "-" in ts_str and ts_str.count("-") > 2:
        ts_str = ts_str.rsplit("-", 1)[0]
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(ts_str)
        return dt.timestamp()
    except Exception:
        return time.time()


def outgoing_to_qq(msg: OutgoingMessage) -> dict[str, Any]:
    """把 OutgoingMessage 转换成 QQ 机器人发送格式。"""
    if msg.format == "markdown":
        content = msg.content
    else:
        content = msg.content
    return {
        "content": content,
    }
