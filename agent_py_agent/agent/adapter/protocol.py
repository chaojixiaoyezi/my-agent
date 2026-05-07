# LLM: External adapter module; keep platform payload and runtime boundary contracts stable.
# 模块用途: 对接 QQ、飞书等外部渠道，把平台事件转换成内部请求。


from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any


# LLM: IncomingMessage 属于外部通道适配的类边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 类用途: 集中保存入站消息字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发通道配置、消息回调和平台输入输出相关副作用，需保持公开契约稳定。
@dataclass
class IncomingMessage:
    """从外部通道收到的统一消息格式。"""

    channel: str
    user_id: str
    content: str
    message_id: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


# LLM: OutgoingMessage 属于外部通道适配的类边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 类用途: 集中保存出站消息字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发通道配置、消息回调和平台输入输出相关副作用，需保持公开契约稳定。
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


# LLM: feishu_to_incoming 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理飞书to入站相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
def feishu_to_incoming(payload: dict[str, Any]) -> IncomingMessage | None:
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


# LLM: outgoing_to_feishu 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理出站to飞书相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
def outgoing_to_feishu(msg: OutgoingMessage) -> dict[str, Any]:
    """把 OutgoingMessage 转换成飞书 webhook 发送格式。"""
    return {
        "msg_type": "text",
        "content": {"text": msg.content},
    }


# ---------------------------------------------------------------------------
# QQ 消息转换
# ---------------------------------------------------------------------------

# LLM: qq_to_incoming 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理QQto入站相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
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


# LLM: _qq_message_fields 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理QQ消息字段相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
def _qq_message_fields(payload: dict[str, Any]) -> tuple[str, str, object, float, dict[str, Any]]:
    d = payload.get("d", {})
    if d:
        return _qq_official_message_fields(d)
    return _qq_legacy_message_fields(payload.get("event", {}))


# LLM: _qq_official_message_fields 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理QQofficial消息字段相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
def _qq_official_message_fields(d: dict[str, Any]) -> tuple[str, str, object, float, dict[str, Any]]:
    author = d.get("author", {})
    user_id = str(author.get("user_openid", "") or author.get("id", "") or d.get("user_id", ""))
    metadata = {
        "qq_guild_id": d.get("guild_id", ""),
        "qq_channel_id": d.get("channel_id", ""),
    }
    return user_id, d.get("content", ""), d.get("id", d.get("msg_id", "")), _qq_timestamp(d), metadata


# LLM: _qq_legacy_message_fields 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理QQlegacy消息字段相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
def _qq_legacy_message_fields(event: dict[str, Any]) -> tuple[str, str, object, float, dict[str, Any]]:
    metadata = {
        "qq_guild_id": event.get("guild_id", ""),
        "qq_channel_id": event.get("channel_id", ""),
    }
    return (
        str(event.get("user_id", "")),
        event.get("content", ""),
        event.get("msg_id", ""),
        float(event.get("timestamp", time.time())),
        metadata,
    )


# LLM: _qq_timestamp 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理QQtimestamp相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
def _qq_timestamp(d: dict[str, Any]) -> float:
    raw_ts = d.get("timestamp", time.time())
    try:
        return _parse_iso_timestamp(raw_ts) if isinstance(raw_ts, str) else float(raw_ts)
    except Exception:
        return time.time()


# LLM: _parse_iso_timestamp 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 解析并归一化isotimestamp的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
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


# LLM: outgoing_to_qq 属于外部通道适配的函数边界；调整时先确认通道配置、消息回调和平台输入输出仍按原契约工作。
# 函数用途: 处理出站toQQ相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持通道配置、消息回调和平台输入输出上的返回值和副作用边界稳定。
def outgoing_to_qq(msg: OutgoingMessage) -> dict[str, Any]:
    """把 OutgoingMessage 转换成 QQ 机器人发送格式。"""
    if msg.format == "markdown":
        content = msg.content
    else:
        content = msg.content
    return {
        "content": content,
    }
