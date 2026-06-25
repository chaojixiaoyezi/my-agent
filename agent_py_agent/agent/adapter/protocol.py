

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .feishu_media import extract_feishu_content


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
    try:
        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {})
        open_id = sender_id.get("open_id", "")

        msg_type = message.get("msg_type", "text")
        # 按类型抽文本+媒体引用(text/post/image/file/audio/...);未知给占位、绝不丢消息
        text, media = extract_feishu_content(msg_type, message.get("content"))
        if not text.strip():
            return None
        metadata: dict[str, Any] = {
            "feishu_chat_id": message.get("chat_id", ""),
            "feishu_msg_type": msg_type,
        }
        if media:  # image_key/file_key/file_name → 供 adapter.fetch_media_to 下载到 agent 工作区
            metadata["media"] = media
        return IncomingMessage(
            channel="feishu",
            user_id=open_id,
            content=text.strip(),
            message_id=message.get("message_id", ""),
            timestamp=float(message.get("create_time", time.time())),
            metadata=metadata,
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
    try:
        user_id, content, msg_id, ts, metadata = _qq_message_fields(payload)
        if not content.strip():
            return None

        return IncomingMessage(
            channel="qq",
            user_id=user_id,
            content=content.strip(),
            message_id=str(msg_id),
            timestamp=ts,
            metadata=metadata,
        )
    except (ValueError, KeyError, TypeError):
        return None


def _qq_message_fields(payload: dict[str, Any]) -> tuple[str, str, object, float, dict[str, Any]]:
    d = payload.get("d", {})
    if not d:
        raise ValueError("missing qq message payload")
    return _qq_official_message_fields(d)


def _qq_official_message_fields(d: dict[str, Any]) -> tuple[str, str, object, float, dict[str, Any]]:
    author = d.get("author", {})
    user_id = str(author.get("user_openid", "") or author.get("id", "") or d.get("user_id", ""))
    metadata = {
        "qq_guild_id": d.get("guild_id", ""),
        "qq_channel_id": d.get("channel_id", ""),
    }
    return user_id, d.get("content", ""), d.get("id", d.get("msg_id", "")), _qq_timestamp(d), metadata


def _qq_timestamp(d: dict[str, Any]) -> float:
    raw_ts = d.get("timestamp", time.time())
    try:
        return _parse_iso_timestamp(raw_ts) if isinstance(raw_ts, str) else float(raw_ts)
    except Exception:
        return time.time()


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
