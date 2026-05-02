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

    QQ 消息事件格式（简化版）：
    {
        "event": {
            "user_id": 123456789,
            "channel_id": 987654321,
            "guild_id": 111111,
            "content": "hello",
            "msg_id": "aaa",
            "timestamp": "1234567890"
        }
    }
    或新版 Open API 格式：
    {
        "d": {
            "author": {"id": "123"},
            "content": "hello",
            "msg_id": "aaa",
            "timestamp": 1234567890
        },
        "s": "READY"
    }
    """
    try:
        # 新版 OneBot 格式（字段在 d 里）
        d = payload.get("d", {})
        if d:
            user_id = str(d.get("author", {}).get("id", d.get("user_id", "")))
            content = d.get("content", "").strip()
            msg_id = d.get("msg_id", "")
            timestamp = float(d.get("timestamp", time.time()))
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
            timestamp = float(event.get("timestamp", time.time()))
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
            timestamp=timestamp,
            metadata=metadata,
        )
    except (ValueError, KeyError, TypeError):
        return None


def outgoing_to_qq(msg: OutgoingMessage) -> dict[str, Any]:
    """把 OutgoingMessage 转换成 QQ 机器人发送格式。"""
    if msg.format == "markdown":
        content = msg.content
    else:
        content = msg.content
    return {
        "content": content,
    }
