# LLM: External adapter module; keep platform payload and runtime boundary contracts stable.
# 模块用途: 对接 QQ、飞书等外部渠道，把平台事件转换成内部请求。


from .base import BaseChannelAdapter
from .feishu import FeishuAdapter
from .manager import ChannelManager
from .protocol import (
    IncomingMessage,
    OutgoingMessage,
    feishu_to_incoming,
    outgoing_to_feishu,
    outgoing_to_qq,
    qq_to_incoming,
)
from .qq import QQAdapter

__all__ = [
    "BaseChannelAdapter",
    "ChannelManager",
    "IncomingMessage",
    "OutgoingMessage",
    "FeishuAdapter",
    "QQAdapter",
    "feishu_to_incoming",
    "outgoing_to_feishu",
    "qq_to_incoming",
    "outgoing_to_qq",
]
