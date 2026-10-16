# LLM: 适配器投影平台结构事实与显式入站受理结果；只有 accepted/稳定 ignored 可完成运输去重，retryable 不授予身份或成功。
# 模块用途: 转换通道消息、校验准确飞书会话地址并声明受理回执；保留原话题身份，不发请求或记录正文诊断。
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .feishu_media import extract_feishu_content


# LLM: 这是运输受理回执，不代表模型执行或外部回复成功；WS/webhook 只能据它提交或释放原去重 claim。
# 类用途: 明确一条飞书事件已经交给消费者、稳定不触发，还是需要供应商重投。
@dataclass(frozen=True)
class FeishuEventDisposition:
    status: str
    reason_code: str = ""

    # LLM: 拒绝未声明状态，避免 None/普通文案被当作已受理；不执行 IO 或修改队列。
    # 函数用途: 校验运输回执状态，确保去重入口没有模糊成功值。
    def __post_init__(self) -> None:
        if self.status not in {"accepted", "ignored", "retryable"}:
            raise ValueError("invalid Feishu event disposition")
        if self.reason_code and not _feishu_reason_identifier(self.reason_code):
            raise ValueError('invalid Feishu disposition reason code')


# LLM: 只传内部原因码给 SDK；异常使 WS 返回失败 ACK，不携带用户正文、供应商响应或凭据。
# 类用途: 通知飞书运输本次尚未受理，需要按原消息编号重投。
class FeishuRetryableEventError(RuntimeError):
    # LLM: SDK 会记录异常 str，必须只携带有界协议标识；不能把 callback/供应商异常正文复制进去。
    # 函数用途: 构造安全的失败 ACK 原因，非法文本收窄成统一未受理码。
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code if _feishu_reason_identifier(reason_code) else 'FEISHU_INGRESS_UNAVAILABLE'
        super().__init__(self.reason_code)


# LLM: 只校验结构化协议标识的字符和上界，不从普通文本推断状态或分类故障。
# 函数用途: 避免异常正文和凭据被误塞到供应商会输出的 reason_code 中。
def _feishu_reason_identifier(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r'FEISHU_[A-Z0-9_]{1,64}', value) is not None


# LLM: 引用仅为诊断投影，不参与权限/路由/去重；不得用此摘要替代原供应商消息编号。
# 函数用途: 为现有日志生成稳定短引用，避免直接输出用户或群聊编号。
def provider_message_reference(value: object) -> str:
    if not value:
        return ''
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class IncomingMessage:
    """从外部通道收到的统一消息格式。"""

    channel: str
    user_id: str
    content: str
    message_id: str
    # 渠道内稳定的会话标识。必须是 chat/thread 维度，不能退化成 user_id；否则同一用户
    # 的多轮会话无法延续，也会把不同群聊或话题串到一起。
    conversation_id: str = ""
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


# LLM: webhook/WS共用内容解析；先规范bot寻址及明确群监控授权的准确平台目标提及，再判敏感命令/落盘；目标ID不授予操作者角色或改变群触发。
# 函数用途: 转换飞书文本、真实授权目标提及和媒体，保留原chat/topic及主体供下游权限核验。
def feishu_to_incoming(payload: dict[str, Any], *, bot_open_id: str = "") -> IncomingMessage | None:
    try:
        event = payload.get("event", {})
        message = event.get("message", {})
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {})
        open_id = sender_id.get("open_id", "")

        # webhook 使用 message_type，既有 WS 归一使用 msg_type；两条入口共用同一内容解析。
        msg_type = message.get("message_type") or message.get("msg_type") or "text"
        # 按类型抽文本+媒体引用(text/post/image/file/audio/...);未知给占位、绝不丢消息
        from .feishu_mentions import (
            command_content_without_bot_mention,
            group_monitor_command_target,
        )

        text, media = extract_feishu_content(msg_type, command_content_without_bot_mention(message, bot_open_id))
        text = group_monitor_command_target(text, message, bot_open_id)
        if not text.strip():
            return None
        metadata: dict[str, Any] = {
            "feishu_chat_id": message.get("chat_id", ""),
            "feishu_root_id": message.get("root_id", ""),
            "feishu_parent_id": message.get("parent_id", ""),
            "feishu_thread_id": message.get("thread_id", ""),
            "feishu_msg_type": msg_type,
            # 会话锁门要分私聊/群聊:群聊永不闲置锁(chat_type=group/p2p,飞书原样带回)。
            "feishu_chat_type": message.get("chat_type", ""),
        }
        if media:  # image_key/file_key/file_name → 供 adapter.fetch_media_to 下载到 agent 工作区
            metadata["media"] = media
        return IncomingMessage(
            channel="feishu",
            user_id=open_id,
            content=text.strip(),
            message_id=message.get("message_id", ""),
            conversation_id=feishu_conversation_id(message),
            timestamp=_feishu_timestamp(message.get("create_time")),
            metadata=metadata,
        )
    except (ValueError, KeyError, TypeError):
        return None


def feishu_conversation_id(message: dict[str, Any]) -> str:
    """返回飞书稳定会话键：普通消息按 chat，话题消息按 chat + root/thread 隔离。"""
    chat_id = str(message.get("chat_id") or "").strip()
    topic_id = str(message.get("thread_id") or message.get("root_id") or "").strip()
    if chat_id and topic_id:
        return f"{chat_id}:thread:{topic_id}"
    return chat_id or topic_id


# LLM: 此解析器只核基础群及固定thread地址结构；topic为有界opaque锚，不枚举前缀或当API消息ID，不授予权限或改actor。
# 函数用途: 取得逻辑会话的原群和可选话题锚；坏地址明确拒绝，调用方仍保留完整会话绑定。
def parse_feishu_conversation_address(value: object) -> tuple[str, str]:
    match = re.fullmatch(r'(oc_[A-Za-z0-9_-]{1,256})(?::thread:([A-Za-z0-9_-]{1,256}))?', value) if isinstance(value, str) else None
    if match is None:
        raise ValueError('CHANNEL_TARGET_INVALID')
    return match.group(1), match.group(2) or ''


# 兼容此前模块内使用者；新入口应导入不带下划线的公共 helper，避免规模化链路
# 复制一份 chat/thread 拼接规则后发生漂移。
_feishu_conversation_id = feishu_conversation_id


def _feishu_timestamp(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return time.time()
    # 飞书 create_time 常用毫秒字符串；内部统一保存 Unix 秒。
    return parsed / 1000.0 if parsed > 10_000_000_000 else parsed


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
            conversation_id=str(metadata.get("qq_channel_id") or user_id),
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
